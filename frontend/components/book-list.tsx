"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { GlassCard } from "@/components/glass";
import { IngestTracePanel } from "@/components/ingest-trace";
import { ScopeChip } from "@/components/scope-chip";
import {
  deleteBook,
  IN_PROGRESS,
  listBooks,
  retryBook,
  setBookShared,
  uploadBook,
  type Book,
} from "@/lib/api/books";
import { ApiRequestError } from "@/lib/api/client";

/**
 * Ingest is asynchronous, so this screen's real job is telling the truth about
 * where a book has got to — including when it failed, and why, with a way out.
 * A spinner that never resolves is the worst possible report of a known failure.
 *
 * Its second job is the share control. Uploading and publishing are deliberately
 * two acts: an upload is private, and "everyone signed in can now read this" is a
 * thing you have to actually choose.
 */

/**
 * The ingest pipeline, named in the reader's language rather than the queue's.
 *
 * The statuses are the backend's own (`books.status`), so this list is the whole
 * journey and cannot drift into describing a step that does not exist. `note` is
 * what is actually happening — a bar that only says "embedding" tells someone who
 * has never heard the word nothing at all.
 */
const STAGES: { status: Book["status"]; label: string; note: string }[] = [
  { status: "uploaded", label: "Queued", note: "Waiting for a worker to pick it up." },
  { status: "parsing", label: "Reading", note: "Pulling the text out, page by page." },
  { status: "chunking", label: "Splitting", note: "Cutting it into passages a citation can point at." },
  { status: "embedding", label: "Indexing", note: "Turning each passage into something searchable." },
  { status: "ready", label: "Ready", note: "Answerable now — ask the tutor about it." },
];

type Tab = "mine" | "shared";

function Progress({ book }: { book: Book }) {
  if (book.status === "failed") {
    return (
      <p className="mt-2 rounded-lg border border-danger/35 bg-danger/10 px-3 py-2 text-xs leading-relaxed text-danger">
        {book.error ?? "Ingestion failed."}
      </p>
    );
  }

  const reached = STAGES.findIndex((stage) => stage.status === book.status);
  const current = STAGES[reached] ?? STAGES[0];
  const done = book.status === "ready";

  return (
    <div className="mt-3.5">
      <div
        className="flex items-center gap-1.5"
        role="progressbar"
        aria-valuemin={1}
        aria-valuemax={STAGES.length}
        aria-valuenow={reached + 1}
        aria-valuetext={`${current.label}: ${current.note}`}
      >
        {STAGES.map((stage, index) => (
          <span
            key={stage.status}
            className={`h-1 flex-1 rounded-full transition-colors ${
              index < reached || done
                ? "bg-saffron/80"
                : // The stage in progress sweeps, so a long parse is visibly working
                  // rather than indistinguishable from a wedged one.
                  index === reached
                  ? "stage-active"
                  : "bg-parchment/12"
            }`}
          />
        ))}
      </div>

      <div className="mt-2 flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
        <span className="font-mono text-[11px] text-parchment-dim">
          {done ? "Done" : `Step ${reached + 1} of ${STAGES.length}`}
        </span>
        <span className="text-xs text-parchment/80">{current.label}</span>
        <span className="text-xs text-parchment-dim">— {current.note}</span>
      </div>
    </div>
  );
}

/**
 * The client-side half of the journey, which no `books` row can describe because the
 * row does not exist until the bytes have landed. An 80 MB book over a slow link is
 * minutes of a spinner that says "Uploading…" and nothing else; this says how far.
 */
function UploadProgress({ name, fraction }: { name: string; fraction: number | null }) {
  const percent = fraction === null ? null : Math.round(fraction * 100);
  const settling = percent !== null && percent >= 100;

  return (
    <div className="mt-4 rounded-xl border border-bark/70 bg-ink/40 p-3.5">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <p className="min-w-0 truncate text-sm text-parchment/85">{name}</p>
        <span className="font-mono text-[11px] text-parchment-dim">
          {settling ? "Handing over…" : percent === null ? "Sending…" : `${percent}%`}
        </span>
      </div>
      <div className="mt-2 h-1 overflow-hidden rounded-full bg-parchment/12">
        <div
          className={`h-full rounded-full bg-indigo transition-[width] duration-200 ${
            percent === null ? "stage-active w-1/3" : ""
          }`}
          style={percent === null ? undefined : { width: `${percent}%` }}
        />
      </div>
      <p className="mt-2 text-xs text-parchment-dim">
        {settling
          ? "The file is with the server; processing starts in a moment."
          : "Sending the file. Reading and indexing happen next, and you can watch both below."}
      </p>
    </div>
  );
}

export function BookList() {
  const [tab, setTab] = useState<Tab>("mine");
  const [books, setBooks] = useState<Book[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  // Name and progress of the file currently going up. Null when nothing is in
  // flight; `fraction` stays null for a body whose length the browser cannot
  // compute, so the bar goes indeterminate rather than inventing a number.
  const [sending, setSending] = useState<{ name: string; fraction: number | null } | null>(null);
  const [pending, setPending] = useState<string | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  const refresh = useCallback(async () => {
    try {
      const page = await listBooks();
      // The API returns everything this caller may see: their own books plus the
      // shared library. The split below is a view, not a permission — the server
      // already decided what is in the list at all.
      setBooks(
        page.items.filter((book) =>
          tab === "mine" ? book.owner_is_me : book.scope === "canon",
        ),
      );
      setError(null);
    } catch (caught) {
      setError(caught instanceof ApiRequestError ? caught.message : "Could not load books.");
      setBooks([]);
    }
  }, [tab]);

  useEffect(() => {
    // Deferred so the state updates land in the promise rather than synchronously
    // inside the effect body.
    const timer = setTimeout(() => void refresh(), 0);
    return () => clearTimeout(timer);
  }, [refresh]);

  // Poll only while something is actually moving. A list of settled books makes no
  // requests at all.
  useEffect(() => {
    if (!books?.some((book) => IN_PROGRESS.includes(book.status))) return;
    const timer = setInterval(() => void refresh(), 2500);
    return () => clearInterval(timer);
  }, [books, refresh]);

  async function upload(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const data = new FormData(form);
    const file = data.get("file") as File | null;
    // Title is optional now: empty means "name it after the file", which the server
    // decides. Only the file is genuinely required — it is the thing being uploaded.
    const title = String(data.get("title") ?? "").trim();
    if (!file || !file.size) return;

    setBusy(true);
    setError(null);
    setSending({ name: file.name, fraction: 0 });
    try {
      await uploadBook(file, title, undefined, (fraction) =>
        setSending({ name: file.name, fraction }),
      );
      form.reset();
      if (fileRef.current) fileRef.current.value = "";
      setTab("mine");
      // Refresh before clearing the upload card, so the reader's eye moves from the
      // client-side bar straight onto the row that took over from it rather than
      // watching an empty gap while the list loads.
      await refresh();
    } catch (caught) {
      setError(caught instanceof ApiRequestError ? caught.message : "Upload failed.");
    } finally {
      setBusy(false);
      setSending(null);
    }
  }

  async function act(id: string, action: () => Promise<unknown>) {
    setError(null);
    setPending(id);
    try {
      await action();
      await refresh();
    } catch (caught) {
      setError(caught instanceof ApiRequestError ? caught.message : "That didn't work.");
    } finally {
      setPending(null);
    }
  }

  return (
    <div className="flex flex-col gap-6">
      <GlassCard raised className="rise p-5 sm:p-6">
        <form onSubmit={upload} className="flex flex-col gap-4 sm:flex-row sm:items-end">
          <label className="flex flex-1 flex-col gap-2">
            <span className="font-mono text-[11px] font-medium tracking-[0.02em] text-parchment-dim">
              Title <span className="opacity-70">— optional</span>
            </span>
            <input
              name="title"
              className="field px-3.5 py-2.5 text-sm"
              placeholder="Named after the file if you leave this empty"
            />
          </label>
          <label className="flex flex-1 flex-col gap-2">
            <span className="font-mono text-[11px] font-medium tracking-[0.02em] text-parchment-dim">
              File
            </span>
            <input
              ref={fileRef}
              name="file"
              type="file"
              required
              accept=".pdf,.docx,.pptx,.txt,.md,.zip"
              className="field px-3.5 py-2 text-sm file:mr-3 file:rounded-md file:border-0 file:bg-parchment/12 file:px-3 file:py-1 file:text-xs file:text-parchment"
            />
          </label>
          <button
            type="submit"
            disabled={busy}
            className="rounded-xl bg-indigo px-5 py-2.5 text-sm font-medium transition hover:bg-indigo/85 disabled:opacity-50"
          >
            {busy ? "Uploading…" : "Add book"}
          </button>
        </form>

        {sending && <UploadProgress name={sending.name} fraction={sending.fraction} />}

        <p className="mt-3 text-xs text-parchment-dim">
          PDF, DOCX, PPTX, TXT or Markdown — or a ZIP of those files, for a book
          downloaded in parts: the parts become one combined book, in order. The file
          type is read from the file itself, not its name.{" "}
          <span className="text-parchment/70">Uploads are private to you</span> until
          you share them.
        </p>
      </GlassCard>

      <div className="flex flex-wrap items-center gap-1">
        {(["mine", "shared"] as const).map((value) => (
          <button
            key={value}
            onClick={() => setTab(value)}
            aria-pressed={tab === value}
            className={`rounded-lg px-3 py-1.5 text-sm transition ${
              tab === value
                ? "bg-parchment/12 text-parchment"
                : "text-parchment-dim hover:text-parchment"
            }`}
          >
            {value === "mine" ? "Your books" : "Shared library"}
          </button>
        ))}
      </div>

      {error && (
        <p
          role="alert"
          className="rounded-xl border border-danger/40 bg-danger/10 px-4 py-3 text-sm text-danger"
        >
          {error}
        </p>
      )}

      {books === null && <p className="text-sm text-parchment-dim">Loading…</p>}

      {books?.length === 0 && (
        <GlassCard className="p-8 text-center">
          <p className="font-display text-xl">
            {tab === "mine" ? "No books yet" : "Nothing shared yet"}
          </p>
          <p className="mt-2 text-sm text-parchment-dim">
            {tab === "mine"
              ? "Add one above and it will be readable in about a minute."
              : "A book appears here once someone shares it."}
          </p>
        </GlassCard>
      )}

      <ul className="flex flex-col gap-3">
        {books?.map((book) => (
          <li key={book.id}>
            <GlassCard className="p-5">
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div className="min-w-0">
                  <div className="flex flex-wrap items-center gap-2.5">
                    <h3 className="font-display text-lg font-semibold">{book.title}</h3>
                    <ScopeChip scope={book.scope} />
                  </div>
                  <p className="mt-1 font-mono text-[11px] text-parchment-dim">
                    {book.source_format}
                    {book.page_count
                      ? ` · ${book.page_count} ${book.page_count === 1 ? "page" : "pages"}`
                      : ""}
                  </p>
                </div>

                {/* Only the owner gets controls. Someone else's shared book is
                    readable and nothing more — the API refuses the rest anyway,
                    and offering a button that 404s is worse than offering none. */}
                {book.owner_is_me && (
                  <div className="flex shrink-0 flex-wrap gap-2">
                    {book.status === "failed" && (
                      <button
                        onClick={() => act(book.id, () => retryBook(book.id))}
                        disabled={pending === book.id}
                        className="rounded-lg border border-saffron/45 px-3 py-1.5 text-xs text-saffron transition hover:bg-saffron/12 disabled:opacity-50"
                      >
                        Try again
                      </button>
                    )}
                    {book.status === "ready" && (
                      <button
                        onClick={() =>
                          act(book.id, () => setBookShared(book.id, book.scope !== "canon"))
                        }
                        disabled={pending === book.id}
                        className={`rounded-lg border px-3 py-1.5 text-xs transition disabled:opacity-50 ${
                          book.scope === "canon"
                            ? "border-canon/45 text-canon hover:bg-canon/12"
                            : "border-parchment/18 text-parchment-dim hover:border-parchment/35 hover:text-parchment"
                        }`}
                      >
                        {book.scope === "canon" ? "Stop sharing" : "Share with everyone"}
                      </button>
                    )}
                    <button
                      onClick={() => act(book.id, () => deleteBook(book.id))}
                      disabled={pending === book.id}
                      className="rounded-lg border border-parchment/18 px-3 py-1.5 text-xs text-parchment-dim transition hover:border-danger/45 hover:text-danger disabled:opacity-50"
                    >
                      Delete
                    </button>
                  </div>
                )}
              </div>
              <Progress book={book} />
              {/* Owner only — the server sends no trace for anybody else's book.
                  Present on a failed book too, where it says which stage it got to. */}
              {book.ingest_trace && <IngestTracePanel trace={book.ingest_trace} />}
            </GlassCard>
          </li>
        ))}
      </ul>
    </div>
  );
}
