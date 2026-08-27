"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import { GlassCard, Eyebrow } from "@/components/glass";
import { listBooks, type Book } from "@/lib/api/books";
import {
  FORMAT_GROUPS,
  FORMAT_META,
  LEVEL_META,
  RIGOR_META,
  formatLabel,
  type CognitiveLevel,
  type QuestionFormat,
  type Rigor,
} from "@/lib/formats";
import {
  createAssessment,
  listAssessments,
  type Assessment,
} from "@/lib/api/assessments";
import { myAttempts } from "@/lib/api/attempts";
import type { AttemptSummary } from "@/lib/api/assessments";
import { ApiRequestError } from "@/lib/api/client";

/**
 * Two lists, because there are two relationships to a paper and neither is a role:
 * papers you wrote, and papers you sat.
 *
 * The form above them is where a paper is specified, and its central decision is that
 * **every part of the specification can be skipped**. Fourteen question formats and six
 * cognitive levels is a lot to put in front of somebody who wants a quiz from a novel,
 * so choosing nothing is a first-class answer: the server picks a mix that suits the
 * one coarse choice they did make. Nobody has to learn what "assertion and reason"
 * means before they can press the button.
 */

const STATUS_LABEL: Record<Assessment["status"], string> = {
  generating: "being written",
  draft: "draft",
  published: "shared",
  closed: "closed",
};

/** A toggle that reads as a tag. `aria-pressed` rather than a hidden checkbox,
 *  because the pressed state is the whole meaning of the control. */
function Chip({
  on,
  title,
  onClick,
  children,
}: {
  on: boolean;
  title?: string;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      aria-pressed={on}
      title={title}
      onClick={onClick}
      className={`rounded-lg border px-3 py-1.5 text-sm transition ${
        on
          ? "border-indigo/60 bg-indigo/12 text-parchment"
          : "border-parchment/15 text-parchment-dim hover:border-parchment/30"
      }`}
    >
      {children}
    </button>
  );
}


function StatusChip({ status }: { status: Assessment["status"] }) {
  const tone =
    status === "published"
      ? "border-canon/45 bg-canon/10 text-canon"
      : status === "generating"
        ? "border-saffron/45 bg-saffron/10 text-saffron"
        : "border-parchment/20 text-parchment-dim";
  return (
    <span className={`rounded-full border px-2.5 py-0.5 font-mono text-[11px] ${tone}`}>
      {STATUS_LABEL[status]}
    </span>
  );
}

export function AssessmentList() {
  const [papers, setPapers] = useState<Assessment[] | null>(null);
  const [sat, setSat] = useState<AttemptSummary[] | null>(null);
  const [books, setBooks] = useState<Book[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [chosen, setChosen] = useState<string[]>([]);
  // Empty is the default and it means auto. Not a placeholder for a choice the author
  // has not made yet — a choice they were allowed not to make.
  const [pickedFormats, setPickedFormats] = useState<QuestionFormat[]>([]);
  const [pickedLevels, setPickedLevels] = useState<CognitiveLevel[]>([]);
  const [rigor, setRigor] = useState<Rigor>("medium");
  const [showPicker, setShowPicker] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const [mine, theirs, shelf] = await Promise.all([
        listAssessments(),
        myAttempts(),
        // Every book the caller can draw from: their own uploads, private or
        // shared, plus anything anyone has shared (D29).
        listBooks(),
      ]);
      setPapers(mine.items);
      setSat(theirs.items);
      setBooks(shelf.items.filter((book) => book.status === "ready"));
      setError(null);
    } catch (caught) {
      setError(caught instanceof ApiRequestError ? caught.message : "Could not load.");
      setPapers([]);
      setSat([]);
    }
  }, []);

  useEffect(() => {
    const timer = setTimeout(() => void refresh(), 0);
    return () => clearTimeout(timer);
  }, [refresh]);

  // Writing a paper is queued and slow. Poll only while one is actually being written.
  useEffect(() => {
    if (!papers?.some((paper) => paper.status === "generating")) return;
    const timer = setInterval(() => void refresh(), 4000);
    return () => clearInterval(timer);
  }, [papers, refresh]);

  async function create(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const data = new FormData(form);
    if (!chosen.length) {
      setError("Choose at least one book to draw from.");
      return;
    }
    // A title is not worth blocking on: left blank, the paper is named after
    // the first book it draws from.
    const firstBook = books.find((book) => chosen.includes(book.id));
    const fallbackTitle = firstBook
      ? chosen.length > 1
        ? `${firstBook.title} (+${chosen.length - 1} more)`
        : firstBook.title
      : "Untitled paper";
    setBusy(true);
    setError(null);
    try {
      await createAssessment({
        title: String(data.get("title") ?? "").trim() || fallbackTitle,
        source: { book_ids: chosen },
        type: String(data.get("type") ?? "mixed") as "mcq" | "subjective" | "mixed",
        formats: pickedFormats,
        levels: pickedLevels,
        rigor,
        instructions: String(data.get("instructions") ?? "").trim() || null,
        question_count: Number(data.get("count") ?? 8),
        duration_minutes: data.get("duration") ? Number(data.get("duration")) : null,
        proctoring_enabled: data.get("proctored") === "on",
      });
      form.reset();
      setChosen([]);
      setPickedFormats([]);
      setPickedLevels([]);
      setRigor("medium");
      setShowPicker(false);
      await refresh();
    } catch (caught) {
      setError(caught instanceof ApiRequestError ? caught.message : "Could not start writing.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex flex-col gap-10">
      {/* ---------------------------------------------------------- create */}
      <GlassCard raised className="rise p-6">
        <Eyebrow>New paper</Eyebrow>
        <p className="mt-2 text-sm leading-relaxed text-parchment-dim">
          Questions are written from the books <strong className="text-parchment">you pick</strong> —
          your own uploads, private or shared, and anything anyone has shared. Nobody
          else&apos;s private books are ever used.
        </p>

        {books.length === 0 ? (
          <p className="mt-5 rounded-xl border border-parchment/12 px-4 py-3 text-sm text-parchment-dim">
            You have no books to draw from yet.{" "}
            <Link href="/books" className="underline underline-offset-4">Upload one</Link>{" "}
            first — it stays private to you unless you share it.
          </p>
        ) : (
          <form onSubmit={create} className="mt-5 flex flex-col gap-5">
            {/* ------------------------------------------------ 1 · the source */}
            <fieldset className="flex flex-col gap-2">
              <legend className="font-mono text-[11px] font-medium tracking-[0.02em] text-parchment-dim">
                Draw from
              </legend>
              <div className="flex flex-wrap gap-2">
                {books.map((book) => {
                  const on = chosen.includes(book.id);
                  const isPrivate = book.scope === "personal";
                  return (
                    <button
                      type="button"
                      key={book.id}
                      aria-pressed={on}
                      onClick={() =>
                        setChosen((current) =>
                          on ? current.filter((id) => id !== book.id) : [...current, book.id],
                        )
                      }
                      className={`rounded-lg border px-3 py-1.5 text-sm transition ${
                        on
                          ? "border-indigo/60 bg-indigo/12 text-parchment"
                          : "border-parchment/15 text-parchment-dim hover:border-parchment/30"
                      }`}
                    >
                      {book.title}
                      {isPrivate && (
                        <span className={`ml-2 font-mono text-[10px] ${on ? "text-saffron" : "text-parchment-dim/70"}`}>
                          private
                        </span>
                      )}
                    </button>
                  );
                })}
              </div>
              {/* The one honest cost of drawing from a private book (D29), said at
                  the moment it becomes true rather than buried in a rule. */}
              {books.some((book) => chosen.includes(book.id) && book.scope === "personal") && (
                <p className="mt-1 rounded-lg border border-saffron/30 bg-saffron/[0.06] px-3 py-2 text-xs leading-relaxed text-parchment-dim">
                  You picked a <span className="text-saffron">private</span> book. It stays
                  private in the library, but the questions written from it will quote and
                  paraphrase it — whoever sits this paper will see that material.
                </p>
              )}
            </fieldset>

            <label className="flex flex-col gap-2">
              <span className="font-mono text-[11px] font-medium tracking-[0.02em] text-parchment-dim">
                Focus (optional)
              </span>
              <input
                name="instructions"
                maxLength={1000}
                className="field px-3.5 py-2.5 text-sm"
                placeholder="A topic or chapters to concentrate on — e.g. photosynthesis, or chapters 3–4"
              />
              <span className="text-xs text-parchment-dim">
                Leave it blank to draw from everything in the books you picked. It steers
                emphasis and wording; every question still comes from the books.
              </span>
            </label>

            {/* ------------------------------------------------ 2 · the paper */}
            <label className="flex flex-col gap-2">
              <span className="font-mono text-[11px] font-medium tracking-[0.02em] text-parchment-dim">
                Title (optional)
              </span>
              <input
                name="title"
                className="field px-3.5 py-2.5 text-sm"
                placeholder="Named after the book if left blank"
              />
            </label>

            <div className="flex flex-wrap gap-4">
              <label className="flex flex-col gap-2">
                <span className="font-mono text-[11px] font-medium tracking-[0.02em] text-parchment-dim">
                  Questions
                </span>
                <input name="count" type="number" min={1} max={50} defaultValue={8} className="field w-28 px-3.5 py-2.5 text-sm" />
              </label>
              <label className="flex flex-col gap-2">
                <span className="font-mono text-[11px] font-medium tracking-[0.02em] text-parchment-dim">
                  Kind
                </span>
                <select name="type" defaultValue="mixed" className="field px-3.5 py-2.5 text-sm">
                  <option value="mixed">Mixed</option>
                  <option value="mcq">Answered by picking</option>
                  <option value="subjective">Written</option>
                </select>
              </label>
              <label className="flex flex-col gap-2">
                <span className="font-mono text-[11px] font-medium tracking-[0.02em] text-parchment-dim">
                  How hard
                </span>
                <select
                  value={rigor}
                  onChange={(event) => setRigor(event.target.value as Rigor)}
                  className="field px-3.5 py-2.5 text-sm"
                >
                  {Object.entries(RIGOR_META).map(([value, label]) => (
                    <option key={value} value={value}>{label}</option>
                  ))}
                </select>
              </label>
              <label className="flex flex-col gap-2">
                <span className="font-mono text-[11px] font-medium tracking-[0.02em] text-parchment-dim">
                  Minutes (optional)
                </span>
                <input name="duration" type="number" min={1} max={600} className="field w-36 px-3.5 py-2.5 text-sm" placeholder="no limit" />
              </label>
            </div>

            {/* A deliberate choice, off by default — watching people is never a
                side effect. The sitter reads a full consent screen before anything
                starts, and a person (the author) reviews whatever the camera
                notes before the sitter sees any of it. */}
            <label className="flex items-start gap-2.5">
              <input name="proctored" type="checkbox" className="mt-0.5 accent-indigo" />
              <span className="text-sm leading-relaxed text-parchment-dim">
                <span className="text-parchment">Proctor this paper.</span> Sitters are
                told before they begin; they are asked to allow their camera and share
                their screen, and you review what the sitting notes before anyone else
                sees anything.
              </span>
            </label>

            {/* --------------------------------------------- the picker ---
                Folded away by default and summarised in one line when it is. An
                author who wants a quiz gets a quiz; an author who wants a match
                grid at `evaluate` level can say so. Neither has to see the other's
                controls. */}
            <div className="rounded-xl border border-parchment/12">
              <button
                type="button"
                aria-expanded={showPicker}
                onClick={() => setShowPicker((open) => !open)}
                className="flex w-full flex-wrap items-center justify-between gap-3 px-4 py-3 text-left"
              >
                <span className="text-sm">
                  Question types
                  <span className="ml-2 text-parchment-dim">
                    {pickedFormats.length === 0
                      ? "chosen to suit the material"
                      : pickedFormats.map(formatLabel).join(", ")}
                  </span>
                </span>
                <span className="font-mono text-[11px] text-parchment-dim">
                  {showPicker ? "hide" : "choose"}
                </span>
              </button>

              {showPicker && (
                <div className="flex flex-col gap-5 border-t border-parchment/10 px-4 py-4">
                  <p className="text-xs leading-relaxed text-parchment-dim">
                    Pick as many as you like, or none — with none chosen, the paper is
                    written in whatever mix suits the books you picked. Not every book
                    supports every type: a novel will not produce a numeric question,
                    and anything the material cannot support is left out rather than
                    invented.
                  </p>

                  {FORMAT_GROUPS.map((group) => (
                    <fieldset key={group.title}>
                      <legend className="font-mono text-[11px] font-medium tracking-[0.02em] text-parchment-dim">
                        {group.title} — {group.hint}
                      </legend>
                      <div className="mt-2 flex flex-wrap gap-2">
                        {group.formats.map((format) => (
                          <Chip
                            key={format}
                            on={pickedFormats.includes(format)}
                            title={FORMAT_META[format].blurb}
                            onClick={() =>
                              setPickedFormats((current) =>
                                current.includes(format)
                                  ? current.filter((value) => value !== format)
                                  : [...current, format],
                              )
                            }
                          >
                            {FORMAT_META[format].label}
                          </Chip>
                        ))}
                      </div>
                    </fieldset>
                  ))}

                  <fieldset>
                    <legend className="font-mono text-[11px] font-medium tracking-[0.02em] text-parchment-dim">
                      What it should test — leave blank for a spread
                    </legend>
                    <div className="mt-2 flex flex-wrap gap-2">
                      {(Object.keys(LEVEL_META) as CognitiveLevel[]).map((level) => (
                        <Chip
                          key={level}
                          on={pickedLevels.includes(level)}
                          title={LEVEL_META[level].blurb}
                          onClick={() =>
                            setPickedLevels((current) =>
                              current.includes(level)
                                ? current.filter((value) => value !== level)
                                : [...current, level],
                            )
                          }
                        >
                          {LEVEL_META[level].label}
                        </Chip>
                      ))}
                    </div>
                  </fieldset>

                  {pickedFormats.length > 0 && (
                    <button
                      type="button"
                      onClick={() => {
                        setPickedFormats([]);
                        setPickedLevels([]);
                      }}
                      className="self-start font-mono text-[11px] text-parchment-dim underline underline-offset-4 hover:text-parchment"
                    >
                      clear and let the material decide
                    </button>
                  )}
                </div>
              )}
            </div>

            <button
              type="submit"
              disabled={busy}
              className="self-start rounded-xl bg-indigo px-5 py-2.5 text-sm font-medium transition hover:bg-indigo/85 disabled:opacity-50"
            >
              {busy ? "Starting…" : "Write it"}
            </button>
          </form>
        )}
      </GlassCard>

      {error && (
        <p role="alert" className="rounded-xl border border-danger/40 bg-danger/10 px-4 py-3 text-sm text-danger">
          {error}
        </p>
      )}

      {/* ------------------------------------------------------ your papers */}
      <section>
        <h2 className="font-display text-xl font-semibold">Your papers</h2>
        {papers?.length === 0 && (
          <p className="mt-3 text-sm text-parchment-dim">None yet.</p>
        )}
        <ul className="mt-4 flex flex-col gap-3">
          {papers?.map((paper) => (
            <li key={paper.id}>
              <Link href={`/assessments/${paper.id}`} className="group block">
                <GlassCard className="p-5 transition group-hover:border-parchment/25">
                  <div className="flex flex-wrap items-start justify-between gap-3">
                    <div className="min-w-0">
                      <div className="flex flex-wrap items-center gap-2.5">
                        <h3 className="font-display text-lg font-semibold">{paper.title}</h3>
                        <StatusChip status={paper.status} />
                      </div>
                      <p className="mt-1 font-mono text-[11px] text-parchment-dim">
                        {paper.question_count} questions
                        {paper.max_score ? ` · ${paper.max_score} marks` : ""}
                        {paper.attempt_count
                          ? ` · ${paper.attempt_count} ${paper.attempt_count === 1 ? "sitting" : "sittings"}`
                          : ""}
                      </p>
                      {paper.formats?.length > 0 && (
                        <p className="mt-1.5 text-[11px] text-parchment-dim">
                          {paper.formats.map(formatLabel).join(" · ")}
                        </p>
                      )}
                      {paper.status === "generating" && (
                        <p className="mt-1.5 font-mono text-[11px] text-saffron">
                          watch it being written →
                        </p>
                      )}
                    </div>
                  </div>
                  {paper.error && (
                    <p className="mt-3 rounded-lg border border-danger/35 bg-danger/10 px-3 py-2 text-xs text-danger">
                      {paper.error}
                    </p>
                  )}
                </GlassCard>
              </Link>
            </li>
          ))}
        </ul>
      </section>

      {/* ------------------------------------------------- papers you've sat */}
      <section>
        <h2 className="font-display text-xl font-semibold">Papers you&apos;ve sat</h2>
        {sat?.length === 0 && (
          <p className="mt-3 text-sm text-parchment-dim">
            None yet. Someone will send you a link.
          </p>
        )}
        <ul className="mt-4 flex flex-col gap-3">
          {sat?.map((attempt) => (
            <li key={attempt.id}>
              <Link href={`/attempt/${attempt.id}/result`} className="group block">
                <GlassCard className="flex flex-wrap items-center justify-between gap-3 p-5 transition group-hover:border-parchment/25">
                  <div>
                    <h3 className="font-display text-lg font-semibold">{attempt.sitter_name}</h3>
                    <p className="mt-1 font-mono text-[11px] text-parchment-dim">
                      {attempt.status === "in_progress" ? "in progress" : "handed in"}
                      {attempt.released ? "" : attempt.graded_at ? " · awaiting release" : ""}
                    </p>
                  </div>
                  {attempt.released ? (
                    <span className="font-mono text-sm">
                      {attempt.score}
                      <span className="text-parchment-dim">/{attempt.max_score}</span>
                    </span>
                  ) : (
                    <span className="font-mono text-[11px] text-parchment-dim">no mark yet</span>
                  )}
                </GlassCard>
              </Link>
            </li>
          ))}
        </ul>
      </section>
    </div>
  );
}
