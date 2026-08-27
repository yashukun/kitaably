/**
 * The question-format vocabulary, frontend side.
 *
 * A deliberate mirror of `backend/app/rag/formats.py`, and only of the parts a browser
 * needs: what to call a format, how to describe it in a picker, and which group it
 * belongs to. The *rules* — which family marks it, how many options it may have, what
 * counts as a valid answer — are not here and must not be copied here. A validation
 * rule duplicated in a browser is a validation rule that will disagree with the server
 * on the day it matters, and the server is the one that decides.
 *
 * `tests/test_formats.py` pins the Python registry against Postgres. Nothing pins this
 * file, so it is kept to labels: the worst a stale label can do is read oddly.
 */

export type QuestionFormat =
  | "mcq"
  | "true_false"
  | "yes_no"
  | "fill_blank"
  | "assertion_reason"
  | "scenario"
  | "flashcard"
  | "multi_select"
  | "match"
  | "sequence"
  | "one_word"
  | "numeric"
  | "short_answer"
  | "long_answer";

/** The grading family. The sitter's answer takes a different shape for each. */
export type QuestionType =
  | "mcq"
  | "multi_select"
  | "short_text"
  | "match"
  | "sequence"
  | "subjective";

export type CognitiveLevel =
  | "recall"
  | "understand"
  | "apply"
  | "analyze"
  | "evaluate"
  | "create";

export type Rigor =
  | "beginner"
  | "easy"
  | "medium"
  | "hard"
  | "expert"
  | "competitive"
  | "interview"
  | "graduate"
  | "research";

export type FormatMeta = { label: string; blurb: string };

export const FORMAT_META: Record<QuestionFormat, FormatMeta> = {
  mcq: { label: "Multiple choice", blurb: "Four options, one right." },
  true_false: { label: "True / false", blurb: "One statement, true or false." },
  yes_no: { label: "Yes / no", blurb: "A closed question, answered yes or no." },
  fill_blank: { label: "Fill in the blank", blurb: "A sentence with a gap." },
  assertion_reason: {
    label: "Assertion & reason",
    blurb: "Two statements, and the link between them.",
  },
  scenario: { label: "Scenario", blurb: "A situation, then the best course of action." },
  flashcard: { label: "Flashcard", blurb: "A term on the front, its meaning on the back." },
  multi_select: { label: "Select all that apply", blurb: "More than one right answer." },
  match: { label: "Match the following", blurb: "Pair each item with its partner." },
  sequence: { label: "Put in order", blurb: "Arrange steps or events." },
  one_word: { label: "One word", blurb: "Typed, marked exactly." },
  numeric: { label: "Numeric", blurb: "A number, marked with a tolerance." },
  short_answer: { label: "Short answer", blurb: "A few sentences, marked to a rubric." },
  long_answer: { label: "Long answer", blurb: "An extended answer, marked to a rubric." },
};

/**
 * How the picker is laid out. Grouped by what the sitter *does*, not by which marking
 * path the server uses — "pick one" and "type it" are the distinction an author is
 * actually making, and it is the one that changes how long the paper takes to sit.
 */
export const FORMAT_GROUPS: { title: string; hint: string; formats: QuestionFormat[] }[] = [
  {
    title: "Pick one",
    hint: "Marked instantly.",
    formats: ["mcq", "true_false", "yes_no", "fill_blank", "flashcard", "assertion_reason", "scenario"],
  },
  {
    title: "Pick several, or arrange",
    hint: "Marked instantly, with part marks.",
    formats: ["multi_select", "match", "sequence"],
  },
  {
    title: "Type it",
    hint: "One word is marked exactly; the longer ones go to a rubric.",
    formats: ["one_word", "numeric", "short_answer", "long_answer"],
  },
];

export const LEVEL_META: Record<CognitiveLevel, FormatMeta> = {
  recall: { label: "Recall", blurb: "Stated outright in the book." },
  understand: { label: "Understand", blurb: "Explain it in your own words." },
  apply: { label: "Apply", blurb: "Use it on a new case." },
  analyze: { label: "Analyse", blurb: "Break it apart; what depends on what." },
  evaluate: { label: "Evaluate", blurb: "Weigh it; is the argument sound." },
  create: { label: "Create", blurb: "Build something new out of it." },
};

export const RIGOR_META: Record<Rigor, string> = {
  beginner: "Beginner",
  easy: "Easy",
  medium: "Medium",
  hard: "Hard",
  expert: "Expert",
  competitive: "Competitive exam",
  interview: "Interview",
  graduate: "Graduate",
  research: "Research",
};

export const formatLabel = (format: QuestionFormat): string =>
  FORMAT_META[format]?.label ?? format;

// ---------------------------------------------------------------- answers
//
// Structured answers travel in the single `response` string as compact JSON, matching
// `answers.response` on the server. Encoding and decoding live here so the runner, the
// result screen and the author's review all read them the same way — and so a decode
// that fails returns an empty answer rather than throwing inside a render.

export function decodeList(response: string | null | undefined): string[] {
  if (!response) return [];
  try {
    const parsed = JSON.parse(response);
    return Array.isArray(parsed) ? parsed.map(String) : [];
  } catch {
    return [];
  }
}

export function decodeMap(response: string | null | undefined): Record<string, string> {
  if (!response) return {};
  try {
    const parsed = JSON.parse(response);
    return parsed && typeof parsed === "object" && !Array.isArray(parsed)
      ? (parsed as Record<string, string>)
      : {};
  } catch {
    return {};
  }
}

export const encode = (value: unknown): string => JSON.stringify(value);
