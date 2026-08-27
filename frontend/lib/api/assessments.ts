import { api, apiDownload, type Page } from "./client";
import type {
  CognitiveLevel,
  QuestionFormat,
  QuestionType,
  Rigor,
} from "@/lib/formats";

export type AssessmentStatus = "draft" | "generating" | "published" | "closed";
export type Grader = "auto" | "llm" | "human";
export type AttemptStatus = "in_progress" | "submitted" | "auto_submitted" | "voided";

/** The cognitive level. Named `Difficulty` because the column is, and renaming it
 *  across the API would be a word's worth of churn. See lib/formats.ts. */
export type Difficulty = CognitiveLevel;

export type Option = { key: string; text: string };

/** Everything a sitter must not see, in one shape. The server does not send this on
 *  the sitting payload, and the database view it would come from does not contain the
 *  column — this type exists for the author's review and the released result. */
export type AnswerKey = {
  correct_options?: string[];
  accepted?: string[];
  tolerance?: number;
  pairs?: Record<string, string>;
  order?: string[];
};

/** What somebody sitting the paper sees. Note the absent fields — the server does not
 *  send them, and the database view they come from does not contain them. */
export type SitQuestion = {
  id: string;
  index: number;
  /** The grading family — decides the shape of the answer. */
  type: QuestionType;
  /** The presentation — decides how the question is drawn. */
  format: QuestionFormat;
  stem: string;
  options: Option[] | null;
  /** The left column of a match grid. Half the question, never the answer. */
  prompt_items: Option[] | null;
  points: number;
  difficulty: Difficulty | null;
};

/** The author's view: everything, including the answer key. */
export type AuthorQuestion = SitQuestion & {
  correct_option: string | null;
  answer_key: AnswerKey | null;
  model_answer: string | null;
  rubric: { criterion: string; points: number }[] | null;
  source_chunk_ids: string[];
  origin: "generated" | "edited" | "written";
};

export type Assessment = {
  id: string;
  title: string;
  /** Derived server-side from the formats, not from what the dropdown said. */
  type: "mcq" | "subjective" | "mixed";
  rigor: Rigor;
  /** What was asked for. Empty means it was generated on auto. */
  formats: QuestionFormat[];
  levels: Difficulty[];
  question_count: number;
  duration_minutes: number | null;
  status: AssessmentStatus;
  results_release: "immediate" | "on_review";
  /** Camera proctoring for everyone who sits this paper. The author's deliberate
   *  choice, off by default. */
  proctoring_enabled: boolean;
  opens_at: string | null;
  closes_at: string | null;
  max_score: number | null;
  error: string | null;
  /** Set when generation succeeded but produced fewer questions than asked for.
   *  A notice, not a failure — the paper is real, it is just short. */
  generation_note: string | null;
  created_at: string;
  updated_at: string;
  /** Only ever present for the author. The token is the access grant, so it is a
   *  credential and the server does not serialise it for anybody else. */
  share_url: string | null;
  attempt_count: number;
};

/** One stage of the generation pipeline. Same triple as the chat trace. */
export type TraceStep = { step: string; detail: string; ms: number };

/**
 * How the paper was written: steps with timings, one `llm` step per model call, and
 * a summary to evaluate performance from. Deliberately content-free — counts,
 * formats, durations and reject reasons, never question text.
 */
export type GenerationTrace = {
  format: string;
  model: string;
  started_at: string;
  /** Null while the run is still live — the worker checkpoints the trace onto
   *  the row after every stage and model call, and null is the "running" flag. */
  finished_at: string | null;
  steps: TraceStep[];
  summary: {
    target: number;
    wall_ms: number;
    llm_ms: number;
    llm_calls: number;
    llm_budget: number;
    accepted: number;
    rejected: number;
    deduped: number;
    final: number;
    per_format: Record<
      string,
      { calls: number; accepted: number; rejected: number; failed_calls: number }
    >;
  };
};

export type AssessmentDetail = Assessment & {
  questions: AuthorQuestion[];
  /** The pipeline trace. Lands with the first checkpoint seconds into a run and
   *  updates after every model call, then is finalised (finished_at set) when
   *  the run completes or fails. */
  trace: GenerationTrace | null;
};

export type AttemptSummary = {
  id: string;
  sitter_name: string | null;
  sitter_email: string;
  status: AttemptStatus;
  started_at: string;
  submitted_at: string | null;
  score: number | null;
  max_score: number | null;
  graded_at: string | null;
  released: boolean;
  grading_error: string | null;
};

export const listAssessments = () => api<Page<Assessment>>("/assessments");

export const getAssessment = (id: string) => api<AssessmentDetail>(`/assessments/${id}`);

export const createAssessment = (body: {
  title: string;
  source: { book_ids: string[]; chapter_ids?: string[] };
  type: "mcq" | "subjective" | "mixed";
  /** Skippable. An empty list means auto — the server picks a mix that suits the
   *  coarse `type`, rather than refusing to write the paper. */
  formats?: QuestionFormat[];
  levels?: Difficulty[];
  rigor?: Rigor;
  instructions?: string | null;
  question_count: number;
  duration_minutes?: number | null;
  results_release?: "immediate" | "on_review";
  proctoring_enabled?: boolean;
}) => api<{ id: string; status: AssessmentStatus }>("/assessments", {
  method: "POST",
  body: JSON.stringify(body),
});

export const publishAssessment = (id: string) =>
  api<Assessment>(`/assessments/${id}/publish`, { method: "POST" });

export const closeAssessment = (id: string) =>
  api<Assessment>(`/assessments/${id}/close`, { method: "POST" });

export const deleteQuestion = (assessmentId: string, questionId: string) =>
  api<void>(`/assessments/${assessmentId}/questions/${questionId}`, { method: "DELETE" });

export const gradebook = (assessmentId: string) =>
  api<Page<AttemptSummary>>(`/assessments/${assessmentId}/attempts`);

export type ExportFormat = "json" | "md";

/**
 * Download the whole paper — questions, answers, rubrics, provenance.
 *
 * Server-rendered from the stored rows rather than from whatever this screen happens
 * to be holding, so an export is complete and current even if the tab is stale.
 *
 * The author only: this is the answer key, and the route is guarded accordingly. The
 * share token is deliberately absent from the file — it is the access grant, so it is
 * a credential, and it belongs on the screen as a link rather than in a document that
 * gets forwarded.
 */
export async function exportAssessment(assessmentId: string, format: ExportFormat) {
  const { blob, filename } = await apiDownload(
    `/assessments/${assessmentId}/export?format=${format}`,
  );
  return { blob, filename: filename ?? `assessment.${format}` };
}
