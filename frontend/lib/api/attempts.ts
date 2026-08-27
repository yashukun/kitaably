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
