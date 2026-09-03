"use client";

import type { Option, SitQuestion } from "@/lib/api/assessments";

/**
 * How a question is answered.
 *
 * One format, one renderer (D32). The switch that used to pick a renderer per grading
 * family is gone, but the *shape* it enforced is not: the component still keys off
 * `question.type`, so a second family added later is a change here rather than a
 * question that renders as the wrong control.
 *
 * Everything here is controlled by one string, because `answers.response` is one
 * column. An mcq's answer is the option key.
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

/** The stem. */
export function QuestionStem({ question }: { question: SitQuestion }) {
  return (
    <p className="mt-3 whitespace-pre-line text-[15px] leading-relaxed">{question.stem}</p>
  );
}

export function QuestionInput({ question, value, onChange, disabled = false }: Props) {
  return <PickOne {...{ question, value, onChange, disabled }} />;
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

/** Whether an answer counts as given, for the "3/10 answered" counter. */
export function isAnswered(question: SitQuestion, value: string): boolean {
  return (value ?? "").trim().length > 0;
}

/** The one place a question's own options are turned into a readable answer.
 *  Used by the result screen and the author's review, never during a sitting.
 *
 *  `format` and `promptItems` stay in the signature though neither is read any more:
 *  both callers pass what the API gave them, and a signature that drops fields the
 *  payload still carries invites the next reader to stop sending them. */
export function renderAnswer(
  format: string,
  options: Option[] | null,
  promptItems: Option[] | null,
  response: string | null,
): string {
  if (!response?.trim()) return "";

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
  model_answer?: string | null;
};

/** The correct answer, as the option list with the right one marked.
 *
 *  Marked in place rather than named: "the answer is B" is not something anybody can
 *  learn from, and this screen is the one a sitter reads after a paper is released. */
export function CorrectAnswer({ question }: { question: KeyShape }) {
  const options = question.options ?? [];
  const correct = question.correct_option;

  if (options.length && correct) {
    return (
      <ul className="mt-4 flex flex-col gap-1.5">
        {options.map((option) => {
          const right = option.key === correct;
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

  // A question written before D32 can still carry a model answer and no options. It is
  // history rather than a live format, but the author's screen should show what is
  // stored rather than a blank where an answer used to be.
  if (question.model_answer) {
    return (
      <p className="mt-4 whitespace-pre-line rounded-lg border border-canon/45 bg-canon/10 px-3.5 py-2 text-sm text-canon">
        {question.model_answer}
      </p>
    );
  }

  return null;
}
