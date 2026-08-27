"use client";

import { useMemo, useRef, useState } from "react";

import type { Option, SitQuestion } from "@/lib/api/assessments";
import { decodeList, decodeMap, encode } from "@/lib/formats";

/**
 * How a question is answered, one renderer per grading family.
 *
 * Fourteen formats, six renderers. The split is the same one the server makes and for
 * the same reason: a true/false really is a two-option multiple choice, so it shares
 * the radio group and cannot drift away from it. What the format changes is the
 * *chrome* — a flashcard is a card, a fill-in-the-blank shows its gap, an assertion
 * and reason keeps its two lines apart.
 *
 * Everything here is controlled by one string, because `answers.response` is one
 * column. Structured answers are JSON inside it: `["A","C"]`, `{"1":"B"}`,
 * `["C","A","B"]`. Encoding lives in lib/formats.ts so this file, the result screen
 * and the author's review cannot disagree about what a saved answer means.
 *
 * No validation lives here. A browser that decided an answer was invalid would be a
 * second, quieter rulebook competing with the server's — and the server is the one
 * that marks the paper.
 */

type Props = {
  question: SitQuestion;
  value: string;
  onChange: (value: string) => void;
  /** A released result renders the same question read-only. */
  disabled?: boolean;
};

const CHOICE = "flex cursor-pointer items-start gap-3 rounded-xl border px-4 py-3 text-sm transition";
const ON = "border-indigo/60 bg-indigo/12";
const OFF = "border-parchment/12 hover:border-parchment/25";

/** The stem, drawn the way its format wants to be read. */
export function QuestionStem({ question }: { question: SitQuestion }) {
  if (question.format === "flashcard") {
    return (
      <div className="mt-3 rounded-2xl border border-saffron/30 bg-saffron/[0.06] px-5 py-7 text-center">
        <p className="font-mono text-[11px] tracking-[0.02em] text-saffron">Front</p>
        <p className="mt-2 font-display text-2xl font-semibold">{question.stem}</p>
      </div>
    );
  }

  if (question.format === "fill_blank") {
    // Split on the run of underscores so the gap can be drawn as a gap. Falls back to
    // the plain stem if the blank is missing — the server refuses to store one without
    // it, but an older row should still render rather than disappear.
    const parts = question.stem.split(/_{2,}/);
    if (parts.length > 1) {
      return (
        <p className="mt-3 text-[15px] leading-relaxed">
          {parts.map((part, index) => (
            <span key={index}>
              {part}
              {index < parts.length - 1 && (
                <span className="mx-1 inline-block min-w-[5rem] border-b-2 border-dashed border-saffron/60 align-baseline" />
              )}
            </span>
          ))}
        </p>
      );
    }
  }

  // Assertion and reason arrive as two labelled lines and must stay that way.
  return (
    <p className="mt-3 whitespace-pre-line text-[15px] leading-relaxed">{question.stem}</p>
  );
}

export function QuestionInput({ question, value, onChange, disabled = false }: Props) {
  switch (question.type) {
    case "mcq":
      return <PickOne {...{ question, value, onChange, disabled }} />;
    case "multi_select":
      return <PickSeveral {...{ question, value, onChange, disabled }} />;
    case "match":
      return <MatchGrid {...{ question, value, onChange, disabled }} />;
    case "sequence":
      return <Arrange {...{ question, value, onChange, disabled }} />;
    case "short_text":
      return <TypedShort {...{ question, value, onChange, disabled }} />;
    default:
      return <TypedLong {...{ question, value, onChange, disabled }} />;
  }
}

// ------------------------------------------------------------------ pick one

function PickOne({ question, value, onChange, disabled }: Props) {
  return (
    <fieldset className="mt-5 flex flex-col gap-2" disabled={disabled}>
      <legend className="sr-only">{question.stem}</legend>
      {(question.options ?? []).map((option) => (
        <label
          key={option.key}
          className={`${CHOICE} ${value === option.key ? ON : OFF} ${
            disabled ? "cursor-default" : ""
          }`}
        >
          <input
            type="radio"
            name={question.id}
            value={option.key}
            checked={value === option.key}
            onChange={() => onChange(option.key)}
            className="mt-0.5 accent-indigo"
          />
          <span>
            <span className="font-mono text-xs text-parchment-dim">{option.key}.</span>{" "}
            {option.text}
          </span>
        </label>
      ))}
    </fieldset>
  );
}

// -------------------------------------------------------------- pick several

function PickSeveral({ question, value, onChange, disabled }: Props) {
  const chosen = useMemo(() => new Set(decodeList(value)), [value]);

  function toggle(key: string) {
    const next = new Set(chosen);
    if (next.has(key)) next.delete(key);
    else next.add(key);
    // Sorted, so the same set of ticks always produces the same stored string and an
    // autosave does not churn on reordering alone.
    onChange(next.size ? encode([...next].sort()) : "");
  }

  return (
    <fieldset className="mt-5 flex flex-col gap-2" disabled={disabled}>
      <legend className="mb-1 font-mono text-[11px] text-parchment-dim">
        More than one answer is correct. A wrong tick cancels a right one.
      </legend>
      {(question.options ?? []).map((option) => (
        <label
          key={option.key}
          className={`${CHOICE} ${chosen.has(option.key) ? ON : OFF} ${
            disabled ? "cursor-default" : ""
          }`}
        >
          <input
            type="checkbox"
            checked={chosen.has(option.key)}
            onChange={() => toggle(option.key)}
            className="mt-0.5 accent-indigo"
          />
          <span>
            <span className="font-mono text-xs text-parchment-dim">{option.key}.</span>{" "}
            {option.text}
          </span>
        </label>
      ))}
    </fieldset>
  );
}

// -------------------------------------------------------------------- match

function MatchGrid({ question, value, onChange, disabled }: Props) {
  const chosen = useMemo(() => decodeMap(value), [value]);
  const right = question.options ?? [];

  function pick(leftKey: string, rightKey: string) {
    const next = { ...chosen };
    if (rightKey) next[leftKey] = rightKey;
    else delete next[leftKey];
    onChange(Object.keys(next).length ? encode(next) : "");
  }

  return (
    <div className="mt-5 flex flex-col gap-5">
      <ul className="flex flex-col gap-2">
        {(question.prompt_items ?? []).map((item) => (
          <li
            key={item.key}
            className="flex flex-wrap items-center gap-3 rounded-xl border border-parchment/12 px-4 py-3"
          >
            <span className="font-mono text-xs text-parchment-dim">{item.key}.</span>
            <span className="min-w-0 flex-1 text-sm">{item.text}</span>
            <select
              aria-label={`Match for ${item.text}`}
              value={chosen[item.key] ?? ""}
              disabled={disabled}
              onChange={(event) => pick(item.key, event.target.value)}
              className="field px-3 py-1.5 text-sm"
            >
              <option value="">—</option>
              {right.map((option) => (
                <option key={option.key} value={option.key}>
                  {option.key}
                </option>
              ))}
            </select>
          </li>
        ))}
      </ul>

      {/* The right-hand bank, spelled out. A dropdown of bare letters is unusable
          without it, and putting the full text inside every dropdown repeats the
          whole column once per row. */}
      <ul className="flex flex-col gap-1.5 rounded-xl border border-parchment/12 bg-ink/40 p-4">
        {right.map((option) => (
          <li key={option.key} className="text-sm">
            <span className="font-mono text-xs text-parchment-dim">{option.key}.</span>{" "}
            {option.text}
          </li>
        ))}
      </ul>
    </div>
  );
}

// ------------------------------------------------------------------ arrange

function Arrange({ question, value, onChange, disabled }: Props) {
  // `?? []` allocates a fresh array on every render, so it cannot be a dependency of
  // the memos below without defeating them entirely.
  const items = useMemo(() => question.options ?? [], [question.options]);
  const byKey = useMemo(
    () => new Map(items.map((option) => [option.key, option])),
    [items],
  );

  // The saved order if there is one, otherwise the order the paper presented — with
  // anything the saved order forgot appended, so a stale answer written against an
  // older draft still renders every item.
  const order = useMemo(() => {
    const saved = decodeList(value).filter((key) => byKey.has(key));
    const missing = items.map((o) => o.key).filter((key) => !saved.includes(key));
    return [...saved, ...missing];
  }, [value, byKey, items]);

  function move(index: number, by: number) {
    const target = index + by;
    if (target < 0 || target >= order.length) return;
    const next = [...order];
    [next[index], next[target]] = [next[target], next[index]];
    onChange(encode(next));
  }

  return (
    <div className="mt-5">
      <p className="mb-2 font-mono text-[11px] text-parchment-dim">
        Use the arrows to arrange them. Nothing is recorded until you move one.
      </p>
      <ol className="flex flex-col gap-2">
        {order.map((key, index) => {
          const option = byKey.get(key);
          if (!option) return null;
          return (
            <li
              key={key}
              className="flex items-center gap-3 rounded-xl border border-parchment/12 px-4 py-3"
            >
              <span className="w-5 font-mono text-xs text-parchment-dim">{index + 1}.</span>
              <span className="min-w-0 flex-1 text-sm">{option.text}</span>
              <span className="flex gap-1">
                <ArrowButton
                  label={`Move ${option.text} up`}
                  disabled={disabled || index === 0}
                  onClick={() => move(index, -1)}
                >
                  ↑
                </ArrowButton>
                <ArrowButton
                  label={`Move ${option.text} down`}
                  disabled={disabled || index === order.length - 1}
                  onClick={() => move(index, 1)}
                >
                  ↓
                </ArrowButton>
              </span>
            </li>
          );
        })}
      </ol>
    </div>
  );
}

/** Buttons rather than drag and drop: dragging is unusable by keyboard, and an exam
 *  is the worst possible place to find that out. */
function ArrowButton({
  label,
  disabled,
  onClick,
  children,
}: {
  label: string;
  disabled: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      aria-label={label}
      disabled={disabled}
      onClick={onClick}
      className="rounded-lg border border-parchment/18 px-2.5 py-1 text-xs text-parchment-dim transition hover:border-parchment/40 hover:text-parchment disabled:opacity-30"
    >
      {children}
    </button>
  );
}

// ------------------------------------------------------------------- typed
//
// Typed answers are typed. Pasting (and dropping text) into an answer field is
// blocked so a written answer is written here, not carried in from somewhere
// else; copying the answer back out is blocked for the same reason. This is a
// UX rule, not a security boundary — the client is untrusted, and during a
// proctored sitting the attempt itself is still recorded as an observation (the
// document-level `paste` listener fires before the default is prevented).

/** Blocks paste/drop/copy on one field and explains itself, once, when tried. */
function useTypedOnly(disabled: boolean) {
  const [tried, setTried] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  function refuse(event: { preventDefault: () => void }) {
    if (disabled) return;
    event.preventDefault();
    setTried(true);
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(() => setTried(false), 4000);
  }

  return {
    tried,
    handlers: {
      onPaste: refuse,
      onDrop: refuse,
      onCopy: refuse,
      onCut: refuse,
      // Browser assistance is text the sitter did not type. Autofill and
      // autocorrect can put words in an answer (autocorrect silently rewrites a
      // one-word answer that is marked by exact comparison); spellcheck is the
      // browser marking the paper before the grader does.
      autoComplete: "off",
      autoCorrect: "off",
      autoCapitalize: "off",
      spellCheck: false,
    },
  };
}

function PasteNotice({ shown }: { shown: boolean }) {
  if (!shown) return null;
  return (
    <p role="status" className="mt-2 font-mono text-[11px] text-saffron">
      Pasting is turned off here — type your answer.
    </p>
  );
}

function TypedShort({ question, value, onChange, disabled }: Props) {
  const numeric = question.format === "numeric";
  const { tried, handlers } = useTypedOnly(disabled ?? false);
  return (
    <div className="mt-5">
      <input
        value={value}
        disabled={disabled}
        inputMode={numeric ? "decimal" : "text"}
        onChange={(event) => onChange(event.target.value)}
        placeholder={numeric ? "A number" : "Your answer"}
        aria-label={question.stem}
        className="field w-full max-w-sm px-4 py-3 text-sm"
        {...handlers}
      />
      <p className="mt-2 font-mono text-[11px] text-parchment-dim">
        {numeric
          ? "The number only — the unit is in the question."
          : "One or two words. Spelling is compared, not guessed at."}
      </p>
      <PasteNotice shown={tried} />
    </div>
  );
}

function TypedLong({ question, value, onChange, disabled }: Props) {
  const { tried, handlers } = useTypedOnly(disabled ?? false);
  return (
    <div className="mt-5">
      <textarea
        value={value}
        disabled={disabled}
        onChange={(event) => onChange(event.target.value)}
        rows={question.format === "long_answer" ? 10 : 5}
        placeholder="Your answer — typed, not pasted…"
        aria-label={question.stem}
        className="field w-full resize-y px-4 py-3 text-sm leading-relaxed"
        {...handlers}
      />
      <PasteNotice shown={tried} />
    </div>
  );
}

/** Whether an answer counts as given, for the "3/10 answered" counter.
 *  A structured answer that decodes to nothing is not an answer. */
export function isAnswered(question: SitQuestion, value: string): boolean {
  const trimmed = (value ?? "").trim();
  if (!trimmed) return false;
  if (question.type === "multi_select" || question.type === "sequence") {
    return decodeList(trimmed).length > 0;
  }
  if (question.type === "match") {
    return Object.keys(decodeMap(trimmed)).length > 0;
  }
  return true;
}

/** The one place a question's own options are turned into a readable answer.
 *  Used by the result screen and the author's review, never during a sitting. */
export function renderAnswer(
  format: string,
  options: Option[] | null,
  promptItems: Option[] | null,
  response: string | null,
): string {
  const text = (list: Option[] | null, key: string) =>
    list?.find((option) => option.key === key)?.text ?? key;

  if (!response?.trim()) return "";
  const list = decodeList(response);
  const map = decodeMap(response);

  if (format === "match") {
    const entries = Object.entries(map);
    if (!entries.length) return response;
    return entries
      .map(([left, right]) => `${text(promptItems, left)} → ${text(options, right)}`)
      .join("; ");
  }
  if (format === "sequence") {
    return list.length ? list.map((key) => text(options, key)).join(" → ") : response;
  }
  if (format === "multi_select") {
    return list.length
      ? list.map((key) => `${key}. ${text(options, key)}`).join("; ")
      : response;
  }
  if (options?.length) {
    const match = options.find((option) => option.key === response.trim().toUpperCase());
    if (match) return `${match.key}. ${match.text}`;
  }
  return response;
}

// ============================================================= the answer key
//
// Drawn for the two audiences entitled to one: the author reviewing a draft, and a
// sitter whose own result has been released. Never rendered during a sitting — the
// payload a sitting is served does not contain any of these fields, and the database
// view it comes from does not contain the columns.

type KeyShape = {
  format: string;
  options?: Option[] | null;
  prompt_items?: Option[] | null;
  correct_option?: string | null;
  answer_key?: {
    correct_options?: string[];
    accepted?: string[];
    tolerance?: number;
    pairs?: Record<string, string>;
    order?: string[];
  } | null;
  model_answer?: string | null;
};

/** The correct answer, in whatever shape this format's answer takes. */
export function CorrectAnswer({ question }: { question: KeyShape }) {
  const key = question.answer_key ?? {};
  const options = question.options ?? [];
  const text = (list: Option[] | null | undefined, wanted: string) =>
    list?.find((option) => option.key === wanted)?.text ?? wanted;

  // Everything answered by picking — one option or several — is drawn as the option
  // list with the right ones marked, because "the answer is B" is not something
  // anybody can learn from.
  const correct = new Set(
    key.correct_options ?? (question.correct_option ? [question.correct_option] : []),
  );
  if (options.length && correct.size) {
    return (
      <ul className="mt-4 flex flex-col gap-1.5">
        {options.map((option) => {
          const right = correct.has(option.key);
          return (
            <li
              key={option.key}
              className={`rounded-lg border px-3.5 py-2 text-sm ${
                right
                  ? "border-canon/45 bg-canon/10 text-canon"
                  : "border-parchment/12 text-parchment/85"
              }`}
            >
              <span className="font-mono text-xs">{option.key}.</span> {option.text}
              {right && <span className="ml-2 font-mono text-[11px]">correct</span>}
            </li>
          );
        })}
      </ul>
    );
  }

  if (key.pairs) {
    return (
      <ul className="mt-4 flex flex-col gap-1.5">
        {Object.entries(key.pairs).map(([left, right]) => (
          <li
            key={left}
            className="rounded-lg border border-canon/45 bg-canon/10 px-3.5 py-2 text-sm text-canon"
          >
            {text(question.prompt_items, left)} → {text(options, right)}
          </li>
        ))}
      </ul>
    );
  }

  if (key.order) {
    return (
      <ol className="mt-4 flex flex-col gap-1.5">
        {key.order.map((itemKey, index) => (
          <li
            key={itemKey}
            className="rounded-lg border border-canon/45 bg-canon/10 px-3.5 py-2 text-sm text-canon"
          >
            <span className="font-mono text-xs">{index + 1}.</span> {text(options, itemKey)}
          </li>
        ))}
      </ol>
    );
  }

  if (key.accepted?.length) {
    return (
      <div className="mt-4">
        <p className="font-mono text-[11px] font-medium tracking-[0.02em] text-parchment-dim">
          Accepted answers
        </p>
        <div className="mt-1.5 flex flex-wrap gap-2">
          {key.accepted.map((accepted) => (
            <span
              key={accepted}
              className="rounded-lg border border-canon/45 bg-canon/10 px-2.5 py-1 font-mono text-xs text-canon"
            >
              {accepted}
            </span>
          ))}
        </div>
        {key.tolerance ? (
          <p className="mt-2 font-mono text-[11px] text-parchment-dim">
            within {key.tolerance * 100}%
          </p>
        ) : null}
      </div>
    );
  }

  if (question.model_answer) {
    return (
      <div className="mt-4">
        <p className="font-mono text-[11px] font-medium tracking-[0.02em] text-parchment-dim">
          Model answer
        </p>
        <p className="mt-1.5 text-sm leading-relaxed text-parchment/85">
          {question.model_answer}
        </p>
      </div>
    );
  }

  return null;
}
