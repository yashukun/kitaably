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
 *
 * One format since D32. The types stay unions of one rather than being deleted: the
 * renderer switching on them is what keeps a second format a change to this file and
 * the components, rather than a change to the wire protocol.
 */

export type QuestionFormat = "mcq";

/** The grading family. One, and it marks arithmetically. */
export type QuestionType = "mcq";

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
};

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
