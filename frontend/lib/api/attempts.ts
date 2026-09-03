import { api, type Page } from "./client";
import type { AnswerKey, AttemptStatus, Grader, Option, SitQuestion } from "./assessments";
import type { QuestionFormat } from "@/lib/formats";

export type ExamPreview = {
  id: string;
  title: string;
  type: "mcq" | "subjective" | "mixed";
  question_count: number;
  duration_minutes: number | null;
  opens_at: string | null;
  closes_at: string | null;
  proctoring_enabled: boolean;
  is_open: boolean;
  already_started: boolean;
};

export type SavedAnswer = { question_id: string; response: string | null };

export type Attempt = {
  id: string;
  assessment_id: string;
  title: string;
  status: AttemptStatus;
  started_at: string;
  /** Server-authoritative. The countdown below is cosmetic; the server refuses a
   *  save after this instant regardless of what the browser believes. */
  deadline_at: string | null;
  /** Tells the runner to open a proctor session and start the camera flow. The
   *  flag alone — nothing evaluative rides along with it. */
  proctoring_enabled: boolean;
  questions: SitQuestion[];
  answers: SavedAnswer[];
};

export type AnswerResult = {
  question_id: string;
  stem: string;
  /** Carried with the mark so a result screen can draw the question rather than
   *  saying the answer was "B" without saying what B was. */
  format: QuestionFormat;
  options: Option[] | null;
  prompt_items: Option[] | null;
  answer_key: AnswerKey | null;
  response: string | null;
  awarded_points: number | null;
  points: number;
  grader: Grader | null;
  feedback: string | null;
  correct_option: string | null;
  model_answer: string | null;
};

export type AttemptResult = {
  id: string;
  assessment_id: string;
  title: string;
  status: AttemptStatus;
  submitted_at: string | null;
  graded_at: string | null;
  /** Explicit rather than implied by a score being present, so the UI cannot render
   *  a mark it was handed for some other reason. */
  released: boolean;
  score: number | null;
  max_score: number | null;
  grading_error: string | null;
  answers: AnswerResult[];
};

export const examPreview = (token: string) => api<ExamPreview>(`/exam/${token}`);

export const startAttempt = (token: string) =>
  api<Attempt>(`/exam/${token}/start`, { method: "POST" });

export const getAttempt = (id: string) => api<Attempt>(`/attempts/${id}`);

export const saveAnswer = (attemptId: string, questionId: string, response: string | null) =>
  api<SavedAnswer>(`/attempts/${attemptId}/answers/${questionId}`, {
    method: "PUT",
    body: JSON.stringify({ response }),
  });

export const submitAttempt = (id: string) =>
  api<AttemptResult>(`/attempts/${id}/submit`, { method: "POST" });

export const attemptResult = (id: string) => api<AttemptResult>(`/attempts/${id}/result`);

export const releaseResult = (id: string) =>
  api<AttemptResult>(`/attempts/${id}/release`, { method: "POST" });

export const overrideGrade = (
  attemptId: string,
  questionId: string,
  awarded_points: number,
  feedback?: string,
) => api<AttemptResult>(`/attempts/${attemptId}/answers/${questionId}/grade`, {
  method: "PATCH",
  body: JSON.stringify({ awarded_points, feedback: feedback ?? null }),
});

export const myAttempts = () => api<Page<import("./assessments").AttemptSummary>>("/attempts");

/** What the author reads before releasing a mark: how the sitting was paced, and
 *  what the camera and the browser observed while it happened.
 *
 *  Author-only by route guard. None of this reaches the person who sat the paper —
 *  and the observations are observations: "no face detected for 42s", never a
 *  conclusion about them. The inference is the reviewer's to make. */
export type ReviewReport = {
  attempt_id: string;
  status: string;
  started_at: string;
  submitted_at: string | null;
  total_seconds: number | null;
  answered: number;
  question_count: number;
  /** Total time / questions. A submission far faster than the paper can be read is an
   *  observation in its own right, and one that needs no camera. */
  seconds_per_question: number | null;
  pace: { question: number; seconds: number | null }[];
  score: number | null;
  max_score: number | null;
  graded_at: string | null;
  released: boolean;
  proctored: boolean;
  /** 0–100, and a queue-ordering device rather than a verdict. Null when the paper
   *  was not proctored. */
  integrity_score: number | null;
  /** The photo taken at consent, before the sitting began. The author compares it with
   *  the stills themselves — the app makes no automated claim about who anybody is. */
  baseline_url: string | null;
  observations: {
    event_id: string;
    occurred_at: string;
    severity: string;
    text: string;
    /** Signed, short-lived, and null when this observation has no photograph. */
    still_url: string | null;
    verdict: string;
  }[];
};

export const reviewReport = (attemptId: string) =>
  api<ReviewReport>(`/attempts/${attemptId}/report`);
