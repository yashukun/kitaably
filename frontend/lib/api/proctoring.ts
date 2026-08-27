import { api } from "./client";

/**
 * The capture path's API surface. Phase 7.
 *
 * Note the shape of what travels: event *types and timings* go up; severity comes
 * back. The server assigns severity from a fixed map and stamps its own clock —
 * nothing this module sends is trusted, and nothing it receives is evaluative.
 * A sitter's own session payload carries cadence and upload targets only.
 */

export type ProctorEventType =
  | "session_start"
  | "session_end"
  | "no_face"
  | "multiple_faces"
  | "face_mismatch"
  | "gaze_away"
  | "head_pose_away"
  | "phone_visible"
  | "tab_blur"
  | "window_blur"
  | "fullscreen_exit"
  | "copy"
  | "paste"
  | "context_menu"
  | "camera_denied"
  | "camera_stopped"
  | "screen_share_denied"
  | "screen_share_stopped"
  | "multiple_displays";

export type ProctorEventIn = {
  /** Pairs this episode with its ack (and its still's upload URL). Never stored. */
  client_ref: string;
  type: ProctorEventType;
  /** This machine's clock — advisory. The server orders by its own. */
  occurred_at: string;
  confidence?: number;
  duration_ms?: number;
  occurrences?: number;
  /** "I captured a still for this episode." Honored only for high severity. */
  has_still?: boolean;
  metadata?: Record<string, unknown>;
};

export type ProctorEventAck = {
  client_ref: string | null;
  accepted: boolean;
  event_id: string | null;
  type: ProctorEventType | null;
  severity: "info" | "low" | "medium" | "high" | null;
  /** Signed, storage-relative. Present only when the server minted an evidence
   *  path for this episode — high severity, still offered. */
  upload_url: string | null;
};

export type ProctorSession = {
  id: string;
  attempt_id: string;
  status: "active" | "closed" | "aborted";
  already_active: boolean;
  baseline_upload_url: string | null;
  heartbeat_interval_seconds: number;
  event_batch_interval_seconds: number;
};

export const openProctorSession = (attemptId: string) =>
  api<ProctorSession>(`/attempts/${attemptId}/proctor-session`, { method: "POST" });

export const recordProctorEvents = (attemptId: string, events: ProctorEventIn[]) =>
  api<{ results: ProctorEventAck[] }>(`/attempts/${attemptId}/proctor-session/events`, {
    method: "POST",
    body: JSON.stringify({ events }),
  });

export const proctorHeartbeat = (attemptId: string) =>
  api<undefined>(`/attempts/${attemptId}/proctor-session/heartbeat`, { method: "POST" });

/**
 * PUT a still to Supabase Storage via its signed upload URL.
 *
 * The URL arrives storage-relative because the backend and this browser reach
 * Supabase at different addresses (locally: a container hostname vs 127.0.0.1).
 * It is resolved here against the browser's own Supabase URL. Bytes go straight
 * to Storage — the API never proxies an image.
 */
export async function uploadEvidence(uploadUrl: string, still: Blob): Promise<boolean> {
  const base = process.env.NEXT_PUBLIC_SUPABASE_URL;
  if (!base) return false;
  try {
    const response = await fetch(`${base}/storage/v1${uploadUrl}`, {
      method: "PUT",
      headers: { "Content-Type": "image/jpeg", "x-upsert": "true" },
      body: still,
    });
    return response.ok;
  } catch {
    // A lost still is a gap in evidence, not a broken exam. The event row it
    // belonged to is already on the server either way.
    return false;
  }
}
