"use client";

import { useState } from "react";

import { type Pipeline } from "@/lib/api/chat";

/**
 * The "Advanced" disclosure on a tutor answer: what actually ran behind it.
 *
 * Closed by default and rendered in-flow — expanding pushes the answer down
 * rather than floating over it, so nothing ever overlaps. It exists only for
 * turns asked in this session: the trace rides the SSE stream and is never
 * persisted, because the transcript records the conversation, not the machinery.
 *
 * Everything shown is the caller's own retrieval over material they can already
 * see — stage timings, the searches that ran, the book vote, and how the turn
 * ended (answered, grounded refusal, "no mention found", …).
 */

const SHAPES: Record<string, string> = {
  focused: "focused search",
  overview: "coverage sample",
  lookup: "mention lookup",
  compare: "cross-book compare",
  metadata: "library record",
};

const OUTCOMES: Record<string, { label: string; tone: string }> = {
  answered: { label: "answered from sources", tone: "text-canon" },
  // The strict search found nothing and the salvage tier did. Amber rather than
  // green: the answer is grounded in real passages, but they were a loose match
  // and the reader should weigh it as one.
  loose: { label: "answered from loose matches", tone: "text-saffron" },
  book_facts: { label: "answered from the record", tone: "text-canon" },
  refusal: { label: "grounded refusal", tone: "text-saffron" },
  no_mentions: { label: "no mention found", tone: "text-saffron" },
  pick_book: { label: "asked which book", tone: "text-saffron" },
  needs_two_books: { label: "needs a second book", tone: "text-saffron" },
  conversational: { label: "no search needed", tone: "text-parchment-dim" },
};

function bookLabel(book: { title: string; chunks?: number; share?: number }): string {
  if (book.share != null) return `${book.title} ${Math.round(book.share * 100)}%`;
  if (book.chunks != null) return `${book.title} ×${book.chunks}`;
  return book.title;
}

export function PipelineDetails({
  pipeline,
  elapsedMs,
  streaming,
}: {
  pipeline: Pipeline;
  elapsedMs?: number;
  streaming?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const outcome = OUTCOMES[pipeline.outcome] ?? {
    label: pipeline.outcome,
    tone: "text-parchment-dim",
  };

  return (
    <div className="mb-3 flex flex-col">
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        aria-expanded={open}
        className="self-end rounded-lg border border-bark/70 px-2.5 py-1 font-mono
                   text-[10px] uppercase tracking-[0.14em] text-parchment-dim
                   transition hover:border-indigo/60 hover:text-parchment"
      >
        Advanced <span aria-hidden>{open ? "▴" : "▾"}</span>
      </button>

      {open && (
        <div className="mt-2 overflow-hidden rounded-xl border border-bark/70 bg-ink/40">
          {/* What ran, in one line: intent · shape · how it ended. */}
          <div
            className="flex flex-wrap items-center gap-x-2 gap-y-1 border-b
                       border-bark/60 px-3.5 py-2 font-mono text-[10px] uppercase
                       tracking-[0.14em]"
          >
            <span className="text-parchment-dim/70">pipeline</span>
            <span className="text-parchment/80">{pipeline.intent}</span>
            {pipeline.shape && (
              <>
                <span className="text-bark" aria-hidden>
                  ·
                </span>
                <span className="text-indigo/90">
                  {SHAPES[pipeline.shape] ?? pipeline.shape}
                </span>
              </>
            )}
            <span className="text-bark" aria-hidden>
              ·
            </span>
            <span className={outcome.tone}>{outcome.label}</span>
          </div>

          {/* When the search ran on something other than what was typed, say so —
              that is exactly the kind of fact this panel exists to surface. */}
          {(pipeline.query || pipeline.topic) && (
            <div className="border-b border-bark/60 px-3.5 py-2 font-mono text-[11px] text-parchment-dim">
              {pipeline.query && (
                <p className="break-words">
                  searched as <span className="text-parchment/85">“{pipeline.query}”</span>
                </p>
              )}
              {pipeline.topic && (
                <p className="break-words">
                  topic <span className="text-parchment/85">“{pipeline.topic}”</span>
                </p>
              )}
            </div>
          )}

          <ol className="px-3.5 py-1.5">
            {pipeline.steps.map((step, index) => (
              <li
                key={`${step.step}-${index}`}
                className="flex items-baseline gap-3 py-1 font-mono text-[11px]"
              >
                <span className="w-16 shrink-0 uppercase tracking-wider text-parchment-dim/80">
                  {step.step}
                </span>
                <span className="min-w-0 flex-1 break-words text-parchment/85">
                  {step.detail}
                </span>
                {step.ms != null && (
                  <span className="shrink-0 tabular-nums text-parchment-dim/70">
                    {step.ms} ms
                  </span>
                )}
              </li>
            ))}
          </ol>

          <div className="border-t border-bark/60 px-3.5 py-2 font-mono text-[11px] text-parchment-dim">
            {pipeline.books.length > 0 && (
              <p className="break-words">
                books · {pipeline.books.map(bookLabel).join("  ·  ")}
              </p>
            )}
            <p className="mt-0.5">
              {pipeline.outcome === "answered" || pipeline.outcome === "loose" ? (
                <>
                  {pipeline.sources} source{pipeline.sources === 1 ? "" : "s"} → tutor ·{" "}
                  {streaming
                    ? "generating…"
                    : elapsedMs != null
                      ? `generated in ${(elapsedMs / 1000).toFixed(1)}s`
                      : "generated"}
                </>
              ) : (
                "fixed reply · no model call"
              )}
            </p>
          </div>
        </div>
      )}
    </div>
  );
}
