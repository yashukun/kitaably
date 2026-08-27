"use client";

import { useState } from "react";

import { formatLabel } from "@/lib/formats";
import type { GenerationTrace } from "@/lib/api/assessments";
import type { QuestionFormat } from "@/lib/formats";

/**
 * The "Advanced" disclosure on a paper: what actually ran when it was written.
 *
 * The sibling of the tutor's pipeline panel, and deliberately the same shape —
 * closed by default, rendered in-flow, a timeline of steps with their laps. The
 * difference is persistence: chat's trace rides the stream and vanishes, this one
 * was recorded by a worker minutes after the request returned, so it comes from the
 * row and survives to be read whenever the question ("why did this take nine
 * minutes", "why is it short") actually gets asked.
 *
 * Everything shown is content-free by the recorder's construction — counts, formats,
 * durations, reject reasons. The questions themselves are on the same screen anyway;
 * what this adds is the machinery, not the material.
 */

function seconds(ms: number): string {
  return ms >= 10_000 ? `${Math.round(ms / 1000)}s` : `${(ms / 1000).toFixed(1)}s`;
}

export function GenerationTracePanel({
  trace,
  defaultOpen = false,
}: {
  trace: GenerationTrace;
  /** Open on mount — used while a run is live, so watching needs no click. */
  defaultOpen?: boolean;
}) {
  const [open, setOpen] = useState(defaultOpen);
  const { summary } = trace;
  // Null finished_at IS the "still running" flag: the worker checkpoints the
  // trace after every stage and call, and only the final write stamps the time.
  const live = trace.finished_at === null;
  const short = summary.final < summary.target;

  return (
    <div className="flex flex-col">
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        aria-expanded={open}
        className="self-end rounded-lg border border-bark/70 px-2.5 py-1 font-mono
                   text-[10px] uppercase tracking-[0.14em] text-parchment-dim
                   transition hover:border-indigo/60 hover:text-parchment"
      >
        {live && (
          <span
            aria-hidden
            className="mr-1.5 inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-saffron align-middle"
          />
        )}
        Advanced <span aria-hidden>{open ? "▴" : "▾"}</span>
      </button>

      {open && (
        <div className="mt-2 overflow-hidden rounded-xl border border-bark/70 bg-ink/40">
          {/* What ran, in one line: model · calls · progress. While live, the
              progress half counts accepted questions against the ask; finished,
              it states the outcome and the wall clock. */}
          <div
            className="flex flex-wrap items-center gap-x-2 gap-y-1 border-b
                       border-bark/60 px-3.5 py-2 font-mono text-[10px] uppercase
                       tracking-[0.14em]"
          >
            <span className="text-parchment-dim/70">generation</span>
            <span className="text-parchment/80">{trace.model}</span>
            <span className="text-bark" aria-hidden>·</span>
            <span className="text-indigo/90">
              {summary.llm_calls}/{summary.llm_budget} model calls
            </span>
            <span className="text-bark" aria-hidden>·</span>
            {live ? (
              <span className="text-saffron">
                {summary.accepted}/{summary.target} accepted · running{" "}
                {seconds(summary.wall_ms)}
              </span>
            ) : (
              <span className={short ? "text-saffron" : "text-canon"}>
                {summary.final}/{summary.target} questions in {seconds(summary.wall_ms)}
              </span>
            )}
          </div>

          <ol className="px-3.5 py-1.5">
            {trace.steps.map((step, index) => (
              <li
                key={`${step.step}-${index}`}
                className="flex items-baseline gap-3 py-1 font-mono text-[11px]"
              >
                <span className="w-16 shrink-0 uppercase tracking-wider text-parchment-dim/80">
                  {step.step}
                </span>
                <span
                  className={`min-w-0 flex-1 break-words ${
                    step.detail.includes("failed:")
                      ? "text-danger/90"
                      : "text-parchment/85"
                  }`}
                >
                  {step.detail}
                </span>
                <span className="shrink-0 tabular-nums text-parchment-dim/70">
                  {step.ms >= 1000 ? seconds(step.ms) : `${step.ms} ms`}
                </span>
              </li>
            ))}
            {live && (
              <li className="flex items-baseline gap-3 py-1 font-mono text-[11px]">
                <span
                  aria-hidden
                  className="w-16 shrink-0 animate-pulse uppercase tracking-wider text-saffron/80"
                >
                  …
                </span>
                <span className="min-w-0 flex-1 animate-pulse text-parchment-dim">
                  the model is writing — the next line lands when its call returns
                </span>
              </li>
            )}
          </ol>

          {/* The performance summary: where the time went and what each format
              earned. `llm_ms` vs `wall_ms` is the number that says whether the
              model or the plumbing is slow — on CPU-only Ollama it is ~99% model,
              which is exactly what this row exists to make visible. */}
          <div className="border-t border-bark/60 px-3.5 py-2 font-mono text-[11px] text-parchment-dim">
            <p>
              model time {seconds(summary.llm_ms)} of {seconds(summary.wall_ms)}
              {live ? " so far" : " total"}
              {summary.rejected > 0 && <> · {summary.rejected} rejected by validation</>}
              {summary.deduped > 0 && <> · {summary.deduped} near-duplicates dropped</>}
            </p>
            <p className="mt-0.5 break-words">
              {Object.entries(summary.per_format).map(([format, tally], index) => (
                <span key={format}>
                  {index > 0 && "  ·  "}
                  <span
                    className={
                      tally.accepted === 0 ? "text-saffron" : "text-parchment/85"
                    }
                  >
                    {formatLabel(format as QuestionFormat)}{" "}
                    {tally.failed_calls === tally.calls
                      ? "failed"
                      : `${tally.accepted}✓${tally.rejected ? ` ${tally.rejected}✗` : ""}`}
                  </span>
                </span>
              ))}
            </p>
          </div>
        </div>
      )}
    </div>
  );
}
