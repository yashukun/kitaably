"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { ChatSidebar } from "@/components/chat-sidebar";
import { GlassCard, Eyebrow } from "@/components/glass";
import { PipelineDetails } from "@/components/pipeline-details";
import { ScopeChip } from "@/components/scope-chip";
import { TutorAnswer } from "@/components/tutor-answer";
import { listBooks, type Book } from "@/lib/api/books";
import {
  askQuestion,
  createChatSession,
  exportConversation,
  listChatSessions,
  listMessages,
  SEARCHES,
  type ChatSession,
  type Citation,
  type ExportFormat,
  type Intent,
  type Pipeline,
} from "@/lib/api/chat";
import { ApiRequestError } from "@/lib/api/client";

type Bubble = {
  key: string;
  role: "user" | "assistant";
  content: string;
  citations: Citation[];
  intent: Intent | null;
  streaming?: boolean;
  failed?: string;
  /** What ran behind this answer. Only live turns carry one — the trace rides the
   *  stream and is not persisted, so reloaded history has nothing to disclose. */
  pipeline?: Pipeline;
  /** Streaming time from the `done` event, for the disclosure's footer. */
  elapsedMs?: number;
};

const message = (caught: unknown, fallback: string) =>
  caught instanceof ApiRequestError ? caught.message : fallback;

/**
 * Citations are rendered as slips beside the answer rather than as footnotes under
 * it, because "go and check the page" is the product, not an afterthought. Each one
 * carries its scope, so the reader always knows whether a sentence came from the
 * shared library or from their own private upload.
 */
function Slips({ id, citations }: { id: string; citations: Citation[] }) {
  if (!citations.length) return null;
  return (
    <div className="mt-5 border-t border-bark/60 pt-4">
      <Eyebrow>Where this came from</Eyebrow>
      <div className="mt-2.5 grid gap-2.5 sm:grid-cols-2">
        {citations.map((citation, index) => (
          <div
            key={citation.chunk_id}
            id={`${id}-cite-${index}`}
            className={`slip p-3.5 scroll-mt-24 ${
              citation.scope === "canon" ? "border-l-canon" : "border-l-personal"
            }`}
          >
            <div className="flex items-center justify-between gap-2">
              <span className="font-mono text-[11px] text-parchment-dim">
                [{index + 1}]{citation.page ? ` page ${citation.page}` : ""}
              </span>
              <ScopeChip scope={citation.scope} />
            </div>
            <p className="mt-1.5 truncate text-sm text-parchment/85">{citation.book_title}</p>
            {citation.genre && (
              <p className="mt-0.5 truncate text-[11px] text-parchment-dim">{citation.genre}</p>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}

/**
 * Download the open conversation as a file. Same interaction language as the
 * book picker: a small toggle that reveals its choices inline, so nothing
 * floats over the transcript. The file comes from the server — the canonical
 * transcript — not from this tab's bubbles, so it is complete even mid-stream
 * or after a reload.
 */
function ExportMenu({
  sessionId,
  onError,
}: {
  sessionId: string;
  onError: (message: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [fetching, setFetching] = useState<ExportFormat | null>(null);

  async function grab(format: ExportFormat) {
    setFetching(format);
    try {
      const { blob, filename } = await exportConversation(sessionId, format);
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = filename;
      anchor.click();
      URL.revokeObjectURL(url);
      setOpen(false);
    } catch (caught) {
      onError(message(caught, "Could not export this conversation."));
    } finally {
      setFetching(null);
    }
  }

  return (
    <div className="flex items-center gap-2">
      {open && (
        <>
          {(["json", "md"] as const).map((format) => (
            <button
              key={format}
              type="button"
              onClick={() => grab(format)}
              disabled={fetching !== null}
              className="rounded-full border border-bark px-3 py-1 text-xs text-parchment-dim
                         transition hover:border-indigo/60 hover:text-parchment
                         disabled:opacity-50"
            >
              {fetching === format ? "…" : format === "json" ? "JSON" : "Markdown"}
            </button>
          ))}
        </>
      )}
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        aria-expanded={open}
        className="rounded-lg border border-bark px-3 py-1.5 text-xs text-parchment-dim
                   transition hover:border-indigo/60 hover:text-parchment"
      >
        Export {open ? "▴" : "▾"}
      </button>
    </div>
  );
}

/**
 * Which books to ask. Nothing selected means all of them, which is the default and
 * the case worth optimising for — picking is a way to *focus* a question, not a
 * prerequisite for asking one.
 *
 * This narrows and cannot widen. The server derives what the reader may see from
 * their session and applies these ids on top, so a stale id for an unshared book
 * simply selects nothing.
 */
function BookPicker({
  books,
  selected,
  onToggle,
  onClear,
}: {
  books: Book[];
  selected: Set<string>;
  onToggle: (id: string) => void;
  onClear: () => void;
}) {
  const [open, setOpen] = useState(false);
  const ready = books.filter((book) => book.status === "ready");
  if (!ready.length) return null;

  const label = selected.size === 0 ? "All my books" : `${selected.size} selected`;

  return (
    <div className="flex flex-col gap-2">
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        className="self-start rounded-lg border border-bark px-3 py-1.5 text-xs
                   text-parchment-dim transition hover:border-indigo/60 hover:text-parchment"
        aria-expanded={open}
      >
        Asking: <span className="text-parchment">{label}</span> {open ? "▴" : "▾"}
      </button>

      {open && (
        <div className="flex flex-wrap gap-2 rounded-xl border border-bark/70 p-3">
          <button
            type="button"
            onClick={onClear}
            className={`rounded-full px-3 py-1 text-xs transition ${
              selected.size === 0
                ? "bg-indigo/30 text-parchment"
                : "border border-bark text-parchment-dim hover:text-parchment"
            }`}
          >
            All my books
          </button>
          {ready.map((book) => (
            <button
              key={book.id}
              type="button"
              onClick={() => onToggle(book.id)}
              title={book.summary ?? undefined}
              className={`rounded-full px-3 py-1 text-xs transition ${
                selected.has(book.id)
                  ? "bg-indigo/30 text-parchment"
                  : "border border-bark text-parchment-dim hover:text-parchment"
              }`}
            >
              {book.title}
              {book.genre && (
                <span className="ml-1.5 text-parchment-dim/70">{book.genre}</span>
              )}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

export function ChatPanel() {
  const [sessions, setSessions] = useState<ChatSession[]>([]);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [bubbles, setBubbles] = useState<Bubble[]>([]);
  const [books, setBooks] = useState<Book[]>([]);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [question, setQuestion] = useState("");
  const [ready, setReady] = useState(false);
  const [loading, setLoading] = useState(false);
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const endRef = useRef<HTMLDivElement>(null);
  // React runs effects twice in development. Without this guard the second pass
  // creates a second empty conversation on every first visit.
  const started = useRef(false);

  useEffect(() => {
    if (started.current) return;
    started.current = true;

    (async () => {
      try {
        const [existing, library] = await Promise.all([listChatSessions(), listBooks()]);
        setBooks(library.items);
        if (existing.items.length) {
          setSessions(existing.items);
          setSessionId(existing.items[0].id);
        } else {
          const created = await createChatSession();
          setSessions([created]);
          setSessionId(created.id);
        }
      } catch (caught) {
        setError(message(caught, "Could not start a chat."));
      } finally {
        setReady(true);
      }
    })();
  }, []);

  // The transcript, every time the conversation changes. This is the whole of "chat
  // remembers": the messages were always in the database, and nothing here used to
  // ask for them, so every reload looked like amnesia.
  useEffect(() => {
    if (!sessionId) return;
    let cancelled = false;

    (async () => {
      // Inside the async body rather than in the effect body: a synchronous setState
      // there cascades a render before the effect has done anything useful.
      setLoading(true);
      try {
        const history = await listMessages(sessionId);
        if (cancelled) return;
        setBubbles(
          history.items.map((row) => ({
            key: row.id,
            role: row.role,
            content: row.content,
            citations: row.citations,
            intent: row.intent,
          })),
        );
      } catch (caught) {
        if (!cancelled) setError(message(caught, "Could not load this conversation."));
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [sessionId]);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [bubbles]);

  const toggleBook = useCallback((id: string) => {
    setSelected((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);

  async function startNew() {
    try {
      const created = await createChatSession();
      setSessions((current) => [created, ...current]);
      setBubbles([]);
      setSessionId(created.id);
    } catch (caught) {
      setError(message(caught, "Could not start a new conversation."));
    }
  }

  async function ask(event: React.FormEvent) {
    event.preventDefault();
    const text = question.trim();
    if (!text || !sessionId || sending) return;

    setQuestion("");
    setSending(true);

    const stamp = `live-${Date.now()}`;
    const answerKey = `${stamp}-a`;

    setBubbles((current) => [
      ...current,
      { key: `${stamp}-q`, role: "user", content: text, citations: [], intent: null },
      {
        key: answerKey,
        role: "assistant",
        content: "",
        citations: [],
        intent: null,
        streaming: true,
      },
    ]);

    const patch = (change: Partial<Bubble>) =>
      setBubbles((current) =>
        current.map((bubble) =>
          bubble.key === answerKey ? { ...bubble, ...change } : bubble,
        ),
      );

    try {
      await askQuestion(
        sessionId,
        text,
        {
          onIntent: (intent) => patch({ intent }),
          onPipeline: (pipeline) => patch({ pipeline }),
          onCitations: (citations) => patch({ citations }),
          onToken: (token) =>
            setBubbles((current) =>
              current.map((bubble) =>
                bubble.key === answerKey
                  ? { ...bubble, content: bubble.content + token }
                  : bubble,
              ),
            ),
          onError: (text) => patch({ failed: text, streaming: false }),
          onDone: (ms) => patch({ streaming: false, elapsedMs: ms }),
        },
        { bookIds: [...selected] },
      );
    } catch (caught) {
      patch({ failed: message(caught, "Something went wrong."), streaming: false });
    } finally {
      setSending(false);
      // The conversation was just named server-side from its first question, and it
      // has moved to the top of the list by activity. Refresh so the switcher agrees.
      listChatSessions()
        .then((rows) => setSessions(rows.items))
        .catch(() => undefined);
    }
  }

  const current = sessions.find((row) => row.id === sessionId);

  return (
    // Two panes at md and up: conversations down the left, the transcript beside
    // them. `minmax(0,1fr)` on the right column rather than `1fr` — a bare 1fr takes
    // its minimum from its content, so one long unbroken token in an answer widens
    // the grid and pushes the sidebar off screen.
    <div className="grid gap-6 md:grid-cols-[15rem_minmax(0,1fr)] md:gap-8">
      <aside className="md:sticky md:top-20 md:self-start">
        <ChatSidebar
          sessions={sessions}
          activeId={sessionId}
          onSelect={setSessionId}
          onNew={startNew}
          busy={sending}
        />
      </aside>

      <div className="flex min-w-0 flex-col gap-6">
        {error && (
          <GlassCard className="p-6">
            <p className="text-sm text-danger">{error}</p>
          </GlassCard>
        )}

        {ready && !error && bubbles.length === 0 && !loading && (
          <GlassCard className="rise p-8">
            <Eyebrow>Start here</Eyebrow>
            <p className="mt-3 font-display text-2xl">Ask anything from your course books.</p>
            <p className="mt-2 max-w-lg text-sm leading-relaxed text-parchment-dim">
              Every answer names the pages it came from. If your books don&apos;t cover
              something, Kitaably says so instead of guessing.
            </p>
          </GlassCard>
        )}

        {sessionId && bubbles.length > 0 && (
          <div className="flex justify-end">
            <ExportMenu sessionId={sessionId} onError={setError} />
          </div>
        )}

        <ol className="flex flex-col gap-5">
          {bubbles.map((bubble) =>
            bubble.role === "user" ? (
              <li key={bubble.key} className="flex">
                <p className="ml-auto max-w-[85%] rounded-2xl rounded-br-md bg-indigo/22 px-4 py-2.5 text-sm">
                  {bubble.content}
                </p>
              </li>
            ) : (
              <li key={bubble.key}>
                <GlassCard className="p-5">
                  {/* The "Advanced" disclosure: what ran behind this answer. It
                      arrives before the first token, so it can be opened while
                      "Reading your books…" is still showing. In-flow, so opening
                      it pushes the answer down rather than covering it. */}
                  {bubble.pipeline && (
                    <PipelineDetails
                      pipeline={bubble.pipeline}
                      elapsedMs={bubble.elapsedMs}
                      streaming={bubble.streaming}
                    />
                  )}
                  {bubble.failed ? (
                    <p className="text-sm text-danger">{bubble.failed}</p>
                  ) : bubble.content ? (
                    <TutorAnswer
                      content={bubble.content}
                      citations={bubble.citations}
                      onOpen={(index) =>
                        document
                          .getElementById(`${bubble.key}-cite-${index}`)
                          ?.scrollIntoView({ behavior: "smooth", block: "center" })
                      }
                    />
                  ) : (
                    <p className="caret text-[15px] text-parchment-dim">
                      {/* Only promise a search when one is actually happening. A
                          greeting never touches the books, and saying it did is a
                          small lie the reader can catch. */}
                      {bubble.intent && !SEARCHES.includes(bubble.intent)
                        ? "Thinking…"
                        : "Reading your books…"}
                    </p>
                  )}
                  <Slips id={bubble.key} citations={bubble.citations} />
                </GlassCard>
              </li>
            ),
          )}
        </ol>
        <div ref={endRef} />

        {/* `relative` so the fade can hang off the top edge; the transcript scrolls
            under this bar, which is why it is `.composer` and not `.glass-raised`. */}
        <form onSubmit={ask} className="sticky bottom-4 mt-2 flex flex-col gap-2">
          <div className="composer-fade" aria-hidden="true" />
          <div className="composer relative flex flex-col gap-2 p-2.5">
            <div className="flex items-center gap-3">
              <input
                value={question}
                onChange={(event) => setQuestion(event.target.value)}
                disabled={!sessionId || sending}
                className="min-w-0 flex-1 border-0 bg-transparent px-3 py-2 text-sm text-parchment
                           placeholder:text-parchment-dim/55 focus:outline-none"
                placeholder={
                  current?.title ? "Ask a follow-up…" : "How do plants make food from light?"
                }
                aria-label="Ask a question about your books"
              />
              <button
                type="submit"
                disabled={!sessionId || sending || !question.trim()}
                className="shrink-0 rounded-xl bg-indigo px-5 py-2.5 text-sm font-medium transition
                           hover:bg-indigo/85 disabled:opacity-40"
              >
                {sending ? "…" : "Ask"}
              </button>
            </div>

            {ready && !error && (
              <BookPicker
                books={books}
                selected={selected}
                onToggle={toggleBook}
                onClear={() => setSelected(new Set())}
              />
            )}
          </div>
        </form>
      </div>
    </div>
  );
}
