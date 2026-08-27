"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import { GlassCard, Eyebrow } from "@/components/glass";
import { CorrectAnswer, renderAnswer } from "@/components/question-input";
import { formatLabel } from "@/lib/formats";
import { attemptResult, type AttemptResult } from "@/lib/api/attempts";
import { ApiRequestError } from "@/lib/api/client";

/**
 * A marked paper.
 *
 * The whole screen keys off `released`, which the server sends explicitly rather than
 * leaving the UI to infer it from a score being present. Before release there is no
 * mark in the payload at all — this is not a component hiding a number it was given.
 */
export function AttemptResultView({ attemptId }: { attemptId: string }) {
  const [result, setResult] = useState<AttemptResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setResult(await attemptResult(attemptId));
      setError(null);
    } catch (caught) {
      setError(caught instanceof ApiRequestError ? caught.message : "Could not load this.");
    }
  }, [attemptId]);

  useEffect(() => {
    const timer = setTimeout(() => void load(), 0);
    return () => clearTimeout(timer);
  }, [load]);

  // Marking is queued, so a paper handed in a second ago has no marks yet. Poll until
  // it does, then stop — a settled result makes no requests.
  useEffect(() => {
    if (!result || result.graded_at || result.status === "voided") return;
    const timer = setInterval(() => void load(), 3000);
    return () => clearInterval(timer);
  }, [result, load]);

  if (error) {
    return (
      <GlassCard className="p-7">
        <p role="alert" className="text-sm text-danger">{error}</p>
      </GlassCard>
    );
  }
  if (!result) return <p className="text-sm text-parchment-dim">Loading…</p>;

  const pending = !result.graded_at && result.status !== "voided";

  return (
    <div className="mx-auto w-full max-w-3xl px-5 py-12 sm:px-8">
      <Eyebrow>Handed in</Eyebrow>
      <h1 className="mt-2 font-display text-3xl font-semibold tracking-tight">{result.title}</h1>

      {result.status === "voided" && (
        <GlassCard className="mt-6 border-saffron/40 p-5">
          <p className="text-sm text-saffron">
            This sitting was voided. Ask whoever set the paper about it.
          </p>
        </GlassCard>
      )}

      {pending && (
        <GlassCard className="mt-6 p-6">
          <p className="font-display text-xl">Being marked</p>
          <p className="mt-2 text-sm leading-relaxed text-parchment-dim">
            Your answers are in. Written questions are marked one at a time, so this can
            take a minute. You can close this page — the result will be here.
          </p>
        </GlassCard>
      )}

      {result.grading_error && (
        <GlassCard className="mt-6 border-danger/40 p-5">
          <p className="text-sm text-danger">{result.grading_error}</p>
        </GlassCard>
      )}

      {!pending && !result.released && result.status !== "voided" && (
        <GlassCard className="mt-6 p-6">
          <p className="font-display text-xl">Marked, not yet released</p>
          <p className="mt-2 text-sm leading-relaxed text-parchment-dim">
            Whoever set this paper reviews the marking before it reaches you. Nothing is
            shown here until they release it.
          </p>
        </GlassCard>
      )}

      {result.released && (
        <>
          <GlassCard raised className="rise mt-6 flex flex-wrap items-baseline justify-between gap-4 p-6">
            <div>
              <Eyebrow>Your mark</Eyebrow>
              <p className="mt-1 font-display text-4xl font-semibold">
                {result.score}
                <span className="text-parchment-dim">/{result.max_score}</span>
              </p>
            </div>
            {result.max_score ? (
              <p className="font-mono text-sm text-parchment-dim">
                {Math.round(((result.score ?? 0) / result.max_score) * 100)}%
              </p>
            ) : null}
          </GlassCard>

          <ol className="mt-6 flex flex-col gap-4">
            {result.answers.map((answer, index) => {
              const full = answer.awarded_points === answer.points;
              const none = (answer.awarded_points ?? 0) === 0;
              return (
                <li key={answer.question_id}>
                  <GlassCard className="p-6">
                    <div className="flex flex-wrap items-baseline justify-between gap-4">
                      <Eyebrow>
                        Question {index + 1} · {formatLabel(answer.format)}
                      </Eyebrow>
                      <span
                        className={`rounded-lg border px-2.5 py-1 font-mono text-xs ${
                          full
                            ? "border-canon/45 bg-canon/10 text-canon"
                            : none
                              ? "border-danger/40 bg-danger/10 text-danger"
                              : "border-saffron/45 bg-saffron/10 text-saffron"
                        }`}
                      >
                        {answer.awarded_points ?? 0}/{answer.points}
                      </span>
                    </div>
                    <p className="mt-3 whitespace-pre-line text-[15px] leading-relaxed">
                      {answer.stem}
                    </p>

                    <div className="mt-4 rounded-xl border border-parchment/12 bg-ink/40 p-4">
                      <p className="font-mono text-[11px] font-medium tracking-[0.02em] text-parchment-dim">
                        You answered
                      </p>
                      <p className="mt-1.5 text-sm leading-relaxed">
                        {renderAnswer(
                          answer.format,
                          answer.options,
                          answer.prompt_items,
                          answer.response,
                        ) || <span className="text-parchment-dim">— nothing —</span>}
                      </p>
                    </div>

                    {/* The right answer, drawn the way the question was. A result that
                        says "the answer was B" without saying what B was teaches
                        nothing, which is the only reason to show a marked paper at
                        all. */}
                    <CorrectAnswer question={answer} />
                    {answer.feedback && (
                      <p className="mt-4 border-l-2 border-indigo/50 pl-4 text-sm leading-relaxed text-parchment/85">
                        {answer.feedback}
                      </p>
                    )}
                    {answer.grader === "human" && (
                      <p className="mt-3 font-mono text-[11px] text-parchment-dim">
                        marked by hand
                      </p>
                    )}
                  </GlassCard>
                </li>
              );
            })}
          </ol>
        </>
      )}

      <p className="mt-8 text-sm">
        <Link href="/assessments" className="text-parchment-dim underline decoration-parchment-dim/30 underline-offset-4 hover:text-parchment">
          Back to assessments
        </Link>
      </p>
    </div>
  );
}
