"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import { GlassCard, Eyebrow } from "@/components/glass";
import { GenerationTracePanel } from "@/components/generation-trace";
import { CorrectAnswer, renderAnswer } from "@/components/question-input";
import { formatLabel } from "@/lib/formats";
import {
  closeAssessment,
  deleteQuestion,
  exportAssessment,
  getAssessment,
  gradebook,
  publishAssessment,
  type AssessmentDetail,
  type AttemptSummary,
  type ExportFormat,
} from "@/lib/api/assessments";
import {
  overrideGrade,
  releaseResult,
  attemptResult,
  reviewReport,
  type AttemptResult,
  type ReviewReport,
} from "@/lib/api/attempts";
import { ApiRequestError } from "@/lib/api/client";

/**
 * The author's screen: review the draft, publish it, then watch the sittings come in.
 *
 * The draft review is the product, not a formality — a generated paper that nobody
 * looked at is a paper the model published. So the answer key, the provenance and the
 * delete control are all here, on the same screen, before the share link exists.
 */

function ShareLink({ url }: { url: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <div className="flex flex-wrap items-center gap-3">
      <code className="min-w-0 flex-1 truncate rounded-lg border border-parchment/18 bg-ink/50 px-3 py-2 font-mono text-xs text-saffron">
        {url}
      </code>
      <button
        onClick={async () => {
          try {
            await navigator.clipboard.writeText(url);
            setCopied(true);
            setTimeout(() => setCopied(false), 1800);
          } catch {
            // Clipboard is permission-gated and blocked outright in some contexts.
            // The link is on screen and selectable either way, so this is not an error
            // worth interrupting anybody about.
          }
        }}
        className="rounded-lg border border-parchment/18 px-3 py-2 text-xs text-parchment-dim transition hover:border-parchment/35 hover:text-parchment"
      >
        {copied ? "Copied" : "Copy"}
      </button>
    </div>
  );
}

/**
 * Download the paper.
 *
 * Two formats because they are for two different things. JSON is the data — every
 * field, versioned, for anything that wants to read the paper back. Markdown is a
 * document: the questions first, the answer key after, so the first half can be
 * printed for a room and the second kept back.
 *
 * The file is built by the server from the stored rows, not from what this screen is
 * holding, so it is complete and current even if the tab is stale.
 */
function ExportPaper({
  assessmentId,
  onError,
}: {
  assessmentId: string;
  onError: (message: string) => void;
}) {
  const [fetching, setFetching] = useState<ExportFormat | null>(null);

  async function grab(format: ExportFormat) {
    setFetching(format);
    try {
      const { blob, filename } = await exportAssessment(assessmentId, format);
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = filename;
      anchor.click();
      URL.revokeObjectURL(url);
    } catch (caught) {
      onError(
        caught instanceof ApiRequestError ? caught.message : "Could not export this paper.",
      );
    } finally {
      setFetching(null);
    }
  }

  return (
    <GlassCard className="flex flex-wrap items-center justify-between gap-4 p-5">
      <div>
        <p className="font-display text-lg">Export this paper</p>
        <p className="mt-1 max-w-lg text-sm leading-relaxed text-parchment-dim">
          Every question with its answer, rubric and the passage it came from. The
          Markdown keeps the answer key in its own section, so the questions can be
          printed on their own. The share link is not in the file — it is the way in to
          the paper, so it stays on this screen.
        </p>
      </div>
      <div className="flex items-center gap-2">
        {(["json", "md"] as const).map((format) => (
          <button
            key={format}
            type="button"
            onClick={() => void grab(format)}
            disabled={fetching !== null}
            className="rounded-lg border border-parchment/18 px-3 py-1.5 text-xs text-parchment-dim transition hover:border-parchment/35 hover:text-parchment disabled:opacity-50"
          >
            {fetching === format ? "…" : format === "json" ? "JSON" : "Markdown"}
          </button>
        ))}
      </div>
    </GlassCard>
  );
}


/**
 * The sitting report: what the author reads before deciding to release a mark.
 *
 * Two halves. How the paper was PACED — total time, and the gap before each answer,
 * which is where an unusually fast run shows up. And what was OBSERVED — the camera
 * and browser events, in the words the server recorded them in.
 *
 * The integrity score is a number for ordering a queue, not a probability that
 * anybody did anything. Nothing here concludes anything about the person: the copy
 * says "no face detected for 42s", never "cheating". That inference belongs to the
 * author, which is the whole reason this screen exists instead of a threshold that
 * voids a paper automatically.
 */
// Stills come back storage-RELATIVE, because the backend and the browser reach
// Supabase at different hostnames. The browser supplies its own origin.
//
// Plain <img>, not next/image, and the lint warning about it is accepted: these are
// short-lived signed URLs into a private bucket, so Next's optimiser would have to
// proxy and cache a photograph of somebody sitting an exam. Expiry is the access
// control here, and a cache outliving it is the thing to avoid.
const stillSrc = (path: string) =>
  `${process.env.NEXT_PUBLIC_SUPABASE_URL ?? ""}/storage/v1${path}`;

function SittingReport({ report }: { report: ReviewReport }) {
  const mmss = (seconds: number | null) =>
    seconds === null
      ? "—"
      : `${Math.floor(seconds / 60)}m ${String(seconds % 60).padStart(2, "0")}s`;

  // Only worth flagging a pace as fast RELATIVE to this sitting: papers differ, and
  // an absolute threshold would call every short answer suspicious.
  const timed = report.pace.filter((p) => p.seconds !== null);
  const median =
    timed.length > 0
      ? [...timed].sort((a, b) => (a.seconds ?? 0) - (b.seconds ?? 0))[
          Math.floor(timed.length / 2)
        ].seconds ?? 0
      : 0;

  return (
    <div className="mt-4 flex flex-col gap-4 rounded-xl border border-parchment/12 p-4">
      <div className="flex flex-wrap gap-6">
        <div>
          <p className="font-mono text-[11px] tracking-[0.02em] text-parchment-dim">
            Time taken
          </p>
          <p className="mt-1 font-display text-xl">{mmss(report.total_seconds)}</p>
        </div>
        <div>
          <p className="font-mono text-[11px] tracking-[0.02em] text-parchment-dim">
            Answered
          </p>
          <p className="mt-1 font-display text-xl">{report.answered}</p>
        </div>
        {report.seconds_per_question !== null && (
          <div>
            <p className="font-mono text-[11px] tracking-[0.02em] text-parchment-dim">
              Per question
            </p>
            <p className="mt-1 font-display text-xl">
              {report.seconds_per_question}s
              <span className="text-parchment-dim">
                {" "}
                &times;{report.question_count}
              </span>
            </p>
          </div>
        )}
        {report.proctored && report.integrity_score !== null && (
          <div>
            <p className="font-mono text-[11px] tracking-[0.02em] text-parchment-dim">
              Integrity score
            </p>
            <p className="mt-1 font-display text-xl">
              {report.integrity_score}
              <span className="text-parchment-dim">/100</span>
            </p>
          </div>
        )}
      </div>

      {timed.length > 0 && (
        <div>
          <p className="font-mono text-[11px] tracking-[0.02em] text-parchment-dim">
            Pace — time before each answer
          </p>
          <div className="mt-2 flex flex-wrap gap-1.5">
            {report.pace.map((step) => {
              const quick = step.seconds !== null && median > 0 && step.seconds < median / 4;
              return (
                <span
                  key={step.question}
                  title={`Question ${step.question}: ${mmss(step.seconds)}`}
                  className={`rounded-md border px-2 py-1 font-mono text-[11px] ${
                    quick
                      ? "border-saffron/45 text-saffron"
                      : "border-parchment/15 text-parchment-dim"
                  }`}
                >
                  Q{step.question} {mmss(step.seconds)}
                </span>
              );
            })}
          </div>
        </div>
      )}

      {report.baseline_url && (
        <div>
          <p className="font-mono text-[11px] tracking-[0.02em] text-parchment-dim">
            Taken before the sitting began
          </p>
          {/* The comparison is YOURS to make. The app stores this and shows it; it
              does not claim the faces match or differ, because a webcam still under
              changing light is thin evidence of identity and a wrong automated call
              here is an accusation. */}
          <img
            src={stillSrc(report.baseline_url)}
            alt="The sitter at the start of the sitting"
            className="mt-2 w-40 rounded-lg border border-parchment/15"
          />
        </div>
      )}

      <div>
        <p className="font-mono text-[11px] tracking-[0.02em] text-parchment-dim">
          What the sitting recorded
        </p>
        {!report.proctored ? (
          <p className="mt-1.5 text-xs text-parchment-dim">
            This paper was not proctored, so there is nothing to review but the marks.
          </p>
        ) : report.observations.length === 0 ? (
          <p className="mt-1.5 text-xs text-parchment-dim">
            Nothing was recorded during this sitting.
          </p>
        ) : (
          <ul className="mt-2 flex flex-col gap-1.5">
            {report.observations.map((item) => (
              <li
                key={item.event_id}
                className={`rounded-lg border px-3 py-2 text-xs ${
                  item.severity === "high"
                    ? "border-saffron/40 bg-saffron/[0.06] text-parchment"
                    : "border-parchment/12 text-parchment-dim"
                }`}
              >
                <div className="flex items-start gap-3">
                  {item.still_url && (
                    <img
                      src={stillSrc(item.still_url)}
                      alt=""
                      className="w-24 shrink-0 rounded-md border border-parchment/15"
                    />
                  )}
                  <span>
                    <span className="font-mono text-[11px] text-parchment-dim">
                      {new Date(item.occurred_at).toLocaleTimeString()}
                    </span>{" "}
                    {item.text}
                  </span>
                </div>
              </li>
            ))}
          </ul>
        )}
        <p className="mt-2 text-[11px] leading-relaxed text-parchment-dim">
          These are observations, not conclusions. A dropped connection and a closed
          laptop look alike from here — what they mean is yours to judge.
        </p>
      </div>
    </div>
  );
}


export function AssessmentReview({ assessmentId }: { assessmentId: string }) {
  const [paper, setPaper] = useState<AssessmentDetail | null>(null);
  const [sittings, setSittings] = useState<AttemptSummary[]>([]);
  const [open, setOpen] = useState<AttemptResult | null>(null);
  const [report, setReport] = useState<ReviewReport | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const detail = await getAssessment(assessmentId);
      setPaper(detail);
      if (detail.status === "published" || detail.status === "closed") {
        setSittings((await gradebook(assessmentId)).items);
      }
      setError(null);
    } catch (caught) {
      setError(caught instanceof ApiRequestError ? caught.message : "Could not load.");
    }
  }, [assessmentId]);

  useEffect(() => {
    const timer = setTimeout(() => void refresh(), 0);
    return () => clearTimeout(timer);
  }, [refresh]);

  useEffect(() => {
    if (paper?.status !== "generating") return;
    const timer = setInterval(() => void refresh(), 4000);
    return () => clearInterval(timer);
  }, [paper?.status, refresh]);

  async function act(action: () => Promise<unknown>) {
    setBusy(true);
    setError(null);
    try {
      await action();
      await refresh();
    } catch (caught) {
      setError(caught instanceof ApiRequestError ? caught.message : "That didn't work.");
    } finally {
      setBusy(false);
    }
  }

  if (error && !paper) {
    return <p role="alert" className="text-sm text-danger">{error}</p>;
  }
  if (!paper) return <p className="text-sm text-parchment-dim">Loading…</p>;

  const editable = paper.status === "draft";

  return (
    <div className="flex flex-col gap-8">
      {error && (
        <p role="alert" className="rounded-xl border border-danger/40 bg-danger/10 px-4 py-3 text-sm text-danger">
          {error}
        </p>
      )}

      {paper.status === "generating" && (
        <GlassCard className="p-6">
          <p className="font-display text-xl">
            <span
              aria-hidden
              className="mr-2.5 inline-block h-2 w-2 animate-pulse rounded-full bg-saffron align-middle"
            />
            Being written
          </p>
          <p className="mt-2 text-sm leading-relaxed text-parchment-dim">
            Questions are drafted from the material a batch at a time, each one is
            checked before it is kept, and whatever the material could not support is
            asked of the formats that worked. On a local model that is several minutes,
            sometimes more than ten. You can leave this page — it carries on without
            you.
          </p>
          <p className="mt-2 text-xs leading-relaxed text-parchment-dim">
            {paper.trace
              ? "The Advanced panel below follows the run live — a new line lands after every model call."
              : "Warming up. The pipeline timeline appears below as soon as the first stage lands."}
          </p>
        </GlassCard>
      )}

      {paper.error && (
        <GlassCard className="border-danger/40 p-5">
          <p className="text-sm text-danger">{paper.error}</p>
        </GlassCard>
      )}

      {/* What actually ran — or is running — for the author who wants to know.
          Present live during a run (the worker checkpoints the trace after every
          call and this screen polls while generating), on success, on a short
          paper, and on outright failure — the failed run is the one whose trace
          somebody most wants to read. Auto-open while live: it is the progress
          report, and progress hidden behind a click is a spinner. */}
      {paper.trace && (
        <GenerationTracePanel
          trace={paper.trace}
          defaultOpen={paper.status === "generating"}
        />
      )}

      {/* A short paper is not a failed one, so this is a notice rather than an error.
          Before it existed, a paper asked for as ten questions and written as one
          arrived looking exactly like a one-question paper somebody meant to write —
          a known outcome reported to nobody. */}
      {paper.generation_note && (
        <GlassCard className="border-saffron/40 p-5">
          <Eyebrow>About this paper</Eyebrow>
          <p className="mt-2 text-sm leading-relaxed text-saffron">
            {paper.generation_note}
          </p>
        </GlassCard>
      )}

      {/* ------------------------------------------------------ publishing */}
      {paper.status === "published" && paper.share_url && (
        <GlassCard raised className="rise p-6">
          <Eyebrow>Share this link</Eyebrow>
          <p className="mt-2 mb-4 text-sm leading-relaxed text-parchment-dim">
            Anyone who opens it can sit the paper — they only need an account, so their
            result has somewhere to come back to. There is nothing else to set up.
          </p>
          <ShareLink url={paper.share_url} />
          <button
            onClick={() => act(() => closeAssessment(paper.id))}
            disabled={busy}
            className="mt-5 rounded-lg border border-parchment/18 px-3 py-1.5 text-xs text-parchment-dim transition hover:border-saffron/45 hover:text-saffron disabled:opacity-50"
          >
            Stop accepting new sittings
          </button>
        </GlassCard>
      )}

      {editable && paper.questions.length > 0 && (
        <GlassCard className="flex flex-wrap items-center justify-between gap-4 p-5">
          <div>
            <p className="font-display text-lg">Ready to share?</p>
            <p className="mt-1 text-sm text-parchment-dim">
              Publishing freezes the paper at {paper.questions.reduce((sum, q) => sum + q.points, 0)} marks
              and creates the link.
            </p>
          </div>
          <button
            onClick={() => act(() => publishAssessment(paper.id))}
            disabled={busy}
            className="rounded-xl bg-indigo px-5 py-2.5 text-sm font-medium transition hover:bg-indigo/85 disabled:opacity-50"
          >
            Publish
          </button>
        </GlassCard>
      )}

      {/* The export sits with the paper rather than with publishing, because it is
          useful at every status — a draft is exactly when an author wants to read the
          whole thing somewhere other than a browser tab. */}
      {paper.questions.length > 0 && (
        <ExportPaper assessmentId={paper.id} onError={setError} />
      )}

      {/* ------------------------------------------------------- questions */}
      {paper.questions.length > 0 && (
        <section>
          <h2 className="font-display text-xl font-semibold">
            The paper
            {editable && (
              <span className="ml-3 font-sans text-xs font-normal text-parchment-dim">
                check every question before you publish
              </span>
            )}
          </h2>
          <ol className="mt-4 flex flex-col gap-4">
            {paper.questions.map((question) => (
              <li key={question.id}>
                <GlassCard className="p-6">
                  <div className="flex flex-wrap items-baseline justify-between gap-3">
                    <Eyebrow>
                      Question {question.index + 1} · {formatLabel(question.format)}
                      {question.difficulty ? ` · ${question.difficulty}` : ""}
                    </Eyebrow>
                    <div className="flex items-center gap-3">
                      <span className="font-mono text-[11px] text-parchment-dim">
                        {question.points} {question.points === 1 ? "mark" : "marks"} · {question.origin}
                      </span>
                      {editable && (
                        <button
                          onClick={() => act(() => deleteQuestion(paper.id, question.id))}
                          disabled={busy}
                          className="rounded-lg border border-parchment/18 px-2.5 py-1 text-xs text-parchment-dim transition hover:border-danger/45 hover:text-danger disabled:opacity-50"
                        >
                          Delete
                        </button>
                      )}
                    </div>
                  </div>

                  <p className="mt-3 whitespace-pre-line text-[15px] leading-relaxed">
                    {question.stem}
                  </p>

                  {/* The left column of a match grid is part of the question, so it
                      belongs above the key rather than inside it. */}
                  {question.prompt_items && question.prompt_items.length > 0 && (
                    <ul className="mt-4 flex flex-col gap-1.5">
                      {question.prompt_items.map((item) => (
                        <li
                          key={item.key}
                          className="rounded-lg border border-parchment/12 px-3.5 py-2 text-sm text-parchment/85"
                        >
                          <span className="font-mono text-xs">{item.key}.</span> {item.text}
                        </li>
                      ))}
                    </ul>
                  )}

                  <CorrectAnswer question={question} />

                  {question.model_answer && question.answer_key && (
                    <div className="mt-4">
                      <p className="font-mono text-[11px] font-medium tracking-[0.02em] text-parchment-dim">
                        Model answer
                      </p>
                      <p className="mt-1.5 text-sm leading-relaxed text-parchment/85">
                        {question.model_answer}
                      </p>
                    </div>
                  )}

                  {question.rubric && question.rubric.length > 0 && (
                    <ul className="mt-3 flex flex-col gap-1">
                      {question.rubric.map((entry, index) => (
                        <li key={index} className="text-xs text-parchment-dim">
                          · {entry.criterion} <span className="font-mono">({entry.points})</span>
                        </li>
                      ))}
                    </ul>
                  )}
                </GlassCard>
              </li>
            ))}
          </ol>
        </section>
      )}

      {/* ------------------------------------------------------- gradebook */}
      {(paper.status === "published" || paper.status === "closed") && (
        <section>
          <h2 className="font-display text-xl font-semibold">Sittings</h2>
          {sittings.length === 0 ? (
            <p className="mt-3 text-sm text-parchment-dim">Nobody has sat it yet.</p>
          ) : (
            <ul className="mt-4 flex flex-col gap-3">
              {sittings.map((sitting) => (
                <li key={sitting.id}>
                  <GlassCard className="p-5">
                    <div className="flex flex-wrap items-center justify-between gap-3">
                      <div>
                        <p className="font-display text-base font-semibold">
                          {sitting.sitter_name ?? sitting.sitter_email}
                        </p>
                        <p className="mt-0.5 font-mono text-[11px] text-parchment-dim">
                          {sitting.status.replace("_", " ")}
                          {sitting.graded_at ? " · marked" : " · not marked yet"}
                          {sitting.released ? " · released" : ""}
                        </p>
                      </div>
                      <div className="flex items-center gap-3">
                        {sitting.score !== null && (
                          <span className="font-mono text-sm">
                            {sitting.score}
                            <span className="text-parchment-dim">/{sitting.max_score}</span>
                          </span>
                        )}
                        {sitting.graded_at && !sitting.released && (
                          <button
                            onClick={() => act(() => releaseResult(sitting.id))}
                            disabled={busy}
                            className="rounded-lg border border-canon/45 px-3 py-1.5 text-xs text-canon transition hover:bg-canon/12 disabled:opacity-50"
                          >
                            Release
                          </button>
                        )}
                        <button
                          onClick={async () => {
                            setReport(
                              report?.attempt_id === sitting.id
                                ? null
                                : await reviewReport(sitting.id),
                            );
                          }}
                          className="rounded-lg border border-parchment/18 px-3 py-1.5 text-xs text-parchment-dim transition hover:border-parchment/35 hover:text-parchment"
                        >
                          {report?.attempt_id === sitting.id ? "Hide report" : "Report"}
                        </button>
                        <button
                          onClick={async () => {
                            setOpen(open?.id === sitting.id ? null : await attemptResult(sitting.id));
                          }}
                          className="rounded-lg border border-parchment/18 px-3 py-1.5 text-xs text-parchment-dim transition hover:border-parchment/35 hover:text-parchment"
                        >
                          {open?.id === sitting.id ? "Hide" : "Open"}
                        </button>
                      </div>
                    </div>

                    {report?.attempt_id === sitting.id && (
                      <SittingReport report={report} />
                    )}
                    {open?.id === sitting.id && (
                      <ol className="mt-5 flex flex-col gap-3 border-t border-parchment/10 pt-5">
                        {open.answers.map((answer) => (
                          <li key={answer.question_id} className="rounded-xl border border-parchment/12 p-4">
                            <p className="text-sm">{answer.stem}</p>
                            <p className="mt-2 text-sm text-parchment/80">
                              <span className="text-parchment-dim">Answered: </span>
                              {renderAnswer(
                                answer.format,
                                answer.options,
                                answer.prompt_items,
                                answer.response,
                              ) || "—"}
                            </p>
                            {answer.feedback && (
                              <p className="mt-2 border-l-2 border-indigo/50 pl-3 text-xs leading-relaxed text-parchment-dim">
                                {answer.feedback}
                              </p>
                            )}
                            <form
                              onSubmit={async (event) => {
                                event.preventDefault();
                                const value = Number(
                                  new FormData(event.currentTarget).get("mark"),
                                );
                                await act(async () => {
                                  const updated = await overrideGrade(
                                    sitting.id,
                                    answer.question_id,
                                    value,
                                  );
                                  setOpen(updated);
                                });
                              }}
                              className="mt-3 flex items-center gap-2"
                            >
                              <input
                                name="mark"
                                type="number"
                                step="0.5"
                                min={0}
                                max={answer.points}
                                defaultValue={answer.awarded_points ?? 0}
                                className="field w-20 px-2.5 py-1.5 text-sm"
                              />
                              <span className="font-mono text-xs text-parchment-dim">
                                / {answer.points}
                              </span>
                              <button className="rounded-lg border border-parchment/18 px-3 py-1.5 text-xs text-parchment-dim transition hover:border-parchment/35 hover:text-parchment">
                                Set mark
                              </button>
                              {answer.grader && (
                                <span className="font-mono text-[11px] text-parchment-dim">
                                  {answer.grader === "human" ? "marked by hand" : `marked ${answer.grader}`}
                                </span>
                              )}
                            </form>
                          </li>
                        ))}
                      </ol>
                    )}
                  </GlassCard>
                </li>
              ))}
            </ul>
          )}
        </section>
      )}

      <p className="text-sm">
        <Link href="/assessments" className="text-parchment-dim underline decoration-parchment-dim/30 underline-offset-4 hover:text-parchment">
          All assessments
        </Link>
      </p>
    </div>
  );
}
