"use client";

import { useState } from "react";

import type { IngestTrace } from "@/lib/api/books";

/**
 * The "Advanced" disclosure on a book: what actually ran while it was being read.
 *
 * The third sibling of the tutor's pipeline panel and the paper's generation trace,
 * and deliberately the same shape — closed by default, in-flow, a timeline of steps
 * with their laps. Ingest happens in a worker minutes after the upload returned, so
 * like generation's it comes from the row rather than from a stream, and survives to
 * be read when the question ("why did this take four minutes", "did it get chapter
 * 7") is actually asked.
 *
 * For a ZIP the manifest is the point: a page count cannot tell you whether all
 * eighteen parts made it in, or what order they were combined in.
 */

function seconds(ms: number): string {
  return ms >= 10_000 ? `${Math.round(ms / 1000)}s` : `${(ms / 1000).toFixed(1)}s`;
}

/** Stages named for the reader, matching the progress bar above the panel. */
const STEP_LABELS: Record<string, string> = {
  download: "fetch",
  unzip: "unzip",
  parse: "read",
  chapters: "chapters",
  chunk: "split",
  embed: "index",
  store: "store",
};

export function IngestTracePanel({ trace }: { trace: IngestTrace }) {
  const [open, setOpen] = useState(false);
  const { summary, manifest } = trace;
  const failed = summary.outcome === "failed";

  return (
    <div className="mt-3 flex flex-col">
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
          {/* What ran, in one line: format · size · how it ended. */}
          <div
            className="flex flex-wrap items-center gap-x-2 gap-y-1 border-b
                       border-bark/60 px-3.5 py-2 font-mono text-[10px] uppercase
                       tracking-[0.14em]"
          >
            <span className="text-parchment-dim/70">ingest</span>
            <span className="text-parchment/80">{trace.source_format}</span>
            <span className="text-bark" aria-hidden>·</span>
            <span className="text-indigo/90">
              {(summary.byte_size / (1024 * 1024)).toFixed(1)} MB
            </span>
            <span className="text-bark" aria-hidden>·</span>
            <span className={failed ? "text-danger" : "text-canon"}>
              {failed ? "failed" : "ready"} in {seconds(summary.wall_ms)}
            </span>
          </div>

          {/* The archive's parts, in the order they were combined. This is the
              answer to "did it find all of them", which nothing else on the card
              can give. */}
          {manifest.length > 0 && (
            <div className="border-b border-bark/60 px-3.5 py-2 font-mono text-[11px]">
              <p className="text-parchment-dim">
                {trace.manifest_total} part{trace.manifest_total === 1 ? "" : "s"}, in
                reading order
                {manifest.length < trace.manifest_total && (
                  <> · showing the first {manifest.length}</>
                )}
              </p>
              <ol className="mt-1.5 flex flex-wrap gap-x-2 gap-y-1">
                {manifest.map((part, index) => (
                  <li key={part.name} className="text-parchment/85">
                    <span className="text-parchment-dim/70 tabular-nums">
                      {index + 1}.
                    </span>{" "}
                    {part.name}
                  </li>
                ))}
              </ol>
            </div>
          )}

          <ol className="px-3.5 py-1.5">
            {trace.steps.map((step, index) => (
              <li
                key={`${step.step}-${index}`}
                className="flex items-baseline gap-3 py-1 font-mono text-[11px]"
              >
                <span className="w-16 shrink-0 uppercase tracking-wider text-parchment-dim/80">
                  {STEP_LABELS[step.step] ?? step.step}
                </span>
                <span className="min-w-0 flex-1 break-words text-parchment/85">
                  {step.detail}
                </span>
                <span className="shrink-0 tabular-nums text-parchment-dim/70">
                  {step.ms >= 1000 ? seconds(step.ms) : `${step.ms} ms`}
                </span>
              </li>
            ))}
          </ol>

          <div className="border-t border-bark/60 px-3.5 py-2 font-mono text-[11px] text-parchment-dim">
            {failed ? (
              <p className="break-words text-danger/90">
                stopped after {trace.steps.length} stage
                {trace.steps.length === 1 ? "" : "s"}
                {summary.reason && <> · {summary.reason}</>}
              </p>
            ) : (
              <p>
                {summary.pages} pages → {summary.chapters} chapter
                {summary.chapters === 1 ? "" : "s"} → {summary.chunks} searchable
                passages · {summary.vectors} vectors indexed
              </p>
            )}
            <p className="mt-0.5 opacity-70">
              {new Date(trace.started_at).toLocaleString()}
            </p>
          </div>
        </div>
      )}
    </div>
  );
}
