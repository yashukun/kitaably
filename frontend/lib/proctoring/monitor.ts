/**
 * The browser-side proctor. Phase 7. (DECISIONS.md D10)
 *
 * Detection runs HERE, on the sitter's machine, at ~5fps — no video is streamed
 * or stored, ever. What leaves the browser is structured events (types, timings,
 * counts) and, for high-severity moments only, one downscaled JPEG still per
 * episode. The server assigns severity and recomputes everything that matters;
 * every number this file produces is advisory by design.
 *
 * **Debouncing is not optional.** A raw detector produces hundreds of events a
 * minute, and a review queue of 400 lines gets rubber-stamped — which destroys
 * the review gate more effectively than removing it. So: a signal must be
 * sustained before it becomes an episode, episodes close only after a grace
 * period, repeats within a window coalesce into one event with an occurrence
 * count, and each type is rate-capped. Thresholds are generous to the sitter:
 * a glance at scratch paper is not a finding.
 *
 * **Failure must not lock the exam.** Camera denial, a model that will not load,
 * a network hole — each degrades to an event or to silence (which the server
 * records as evidence by itself). Nothing here may ever block answering.
 */

import {
  recordProctorEvents,
  uploadEvidence,
  proctorHeartbeat,
  type ProctorEventIn,
  type ProctorEventType,
  type ProctorSession,
} from "@/lib/api/proctoring";
import { ApiRequestError } from "@/lib/api/client";

// Pinned together: the npm package (bundled JS API) and the wasm it loads at
// runtime must be the same version. The models are Google's hosted releases —
// detection quietly degrades to DOM-only signals if either host is unreachable.
const MEDIAPIPE_VERSION = "0.10.14";
const WASM_BASE = `https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@${MEDIAPIPE_VERSION}/wasm`;
const FACE_MODEL_URL =
  "https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/face_landmarker.task";
const OBJECT_MODEL_URL =
  "https://storage.googleapis.com/mediapipe-models/object_detector/efficientdet_lite0/float16/1/efficientdet_lite0.tflite";

const DETECT_INTERVAL_MS = 200; // ~5fps for face signals
const OBJECT_EVERY_NTH_TICK = 5; // ~1fps for the (weak) phone detector
const STILL_MAX_WIDTH = 640;
const STILL_JPEG_QUALITY = 0.7;
const QUEUE_CAP = 60;

/** Sustain / close / coalesce / rate-cap per signal. minMs is how long a
 *  condition must hold before it is an episode at all. */
const SIGNALS: Record<
  string,
  { minMs: number; graceMs: number; coalesceMs: number; maxPerMin: number; still?: boolean }
> = {
  // `still: true` on everything an author would want to SEE rather than read about.
  // A line saying "no face for 42s" and a photograph of an empty chair are different
  // kinds of evidence, and the second is what makes a review defensible. The
  // per-minute cap below is what stops a camera pointed at a wall uploading hundreds.
  no_face: { minMs: 3000, graceMs: 1500, coalesceMs: 30_000, maxPerMin: 4, still: true },
  multiple_faces: { minMs: 2000, graceMs: 1500, coalesceMs: 30_000, maxPerMin: 4, still: true },
  head_pose_away: { minMs: 5000, graceMs: 2000, coalesceMs: 30_000, maxPerMin: 3, still: true },
  phone_visible: { minMs: 2000, graceMs: 2000, coalesceMs: 30_000, maxPerMin: 3, still: true },
  tab_blur: { minMs: 500, graceMs: 0, coalesceMs: 30_000, maxPerMin: 4 },
  window_blur: { minMs: 500, graceMs: 0, coalesceMs: 30_000, maxPerMin: 4 },
  copy: { minMs: 0, graceMs: 0, coalesceMs: 30_000, maxPerMin: 4 },
  paste: { minMs: 0, graceMs: 0, coalesceMs: 30_000, maxPerMin: 4 },
  context_menu: { minMs: 0, graceMs: 0, coalesceMs: 30_000, maxPerMin: 4 },
  // Sustained, generously: plugging a projector in for a second must not be an
  // episode, and one long "a second display was connected" line with a duration
  // tells the author more than twenty short ones.
  multiple_displays: { minMs: 5000, graceMs: 3000, coalesceMs: 60_000, maxPerMin: 2 },
};

const DISPLAY_CHECK_MS = 2000;

// A continuously-true condition (camera at the wall) still surfaces while it
// runs: an open episode past this emits a checkpoint with the duration so far.
const CHECKPOINT_MS = 120_000;

type Episode = {
  type: ProctorEventType;
  startedAt: number;
  endedAt: number;
  durationMs: number;
  occurrences: number;
  confidence?: number;
  still?: Blob;
};

type Tracker = {
  activeSince: number | null;
  inactiveSince: number | null;
  checkpointedAt: number | null;
  pending: Episode | null;
  emitted: number[]; // timestamps, for the per-minute cap
  confidence?: number;
};

/** One frame drawn small. Returns null when the video has no frames to give. */
export async function captureStill(video: HTMLVideoElement): Promise<Blob | null> {
  if (video.readyState < 2 || !video.videoWidth) return null;
  const scale = Math.min(1, STILL_MAX_WIDTH / video.videoWidth);
  const canvas = document.createElement("canvas");
  canvas.width = Math.round(video.videoWidth * scale);
  canvas.height = Math.round(video.videoHeight * scale);
  const context = canvas.getContext("2d");
  if (!context) return null;
  context.drawImage(video, 0, 0, canvas.width, canvas.height);
  return new Promise((resolve) =>
    canvas.toBlob(resolve, "image/jpeg", STILL_JPEG_QUALITY),
  );
}

export class ProctorMonitor {
  private attemptId: string;
  /** Neutral, technical status for the sitter: "camera stopped", never a
   *  judgement. Nothing evaluative may ever travel through this callback. */
  private onNotice: (notice: string | null) => void;
  /** Equipment state for the runner's chrome (the re-share button). Carries a
   *  boolean about hardware, never anything evaluative. */
  private onScreenChange: (active: boolean) => void;

  private video: HTMLVideoElement | null = null;
  private trackers = new Map<string, Tracker>();
  private queue: ProctorEventIn[] = [];
  private stills = new Map<string, Blob>();
  private refCounter = 0;

  private detectTimer: ReturnType<typeof setInterval> | null = null;
  private flushTimer: ReturnType<typeof setInterval> | null = null;
  private heartbeatTimer: ReturnType<typeof setInterval> | null = null;
  private displayTimer: ReturnType<typeof setInterval> | null = null;
  private screenStream: MediaStream | null = null;
  private tick = 0;
  private stopped = false;
  private flushing = false;
  private domCleanup: (() => void)[] = [];

  // Loaded lazily; either may be null forever if the CDN is unreachable.
  private faceLandmarker: { detectForVideo: (v: HTMLVideoElement, t: number) => FaceResult } | null = null;
  private objectDetector: { detectForVideo: (v: HTMLVideoElement, t: number) => ObjectResult } | null = null;

  constructor(
    attemptId: string,
    onNotice: (notice: string | null) => void = () => {},
    onScreenChange: (active: boolean) => void = () => {},
  ) {
    this.attemptId = attemptId;
    this.onNotice = onNotice;
    this.onScreenChange = onScreenChange;
  }

  /** Begin monitoring. `video` is the runner's self-view element, already
   *  attached to the camera stream (or to nothing, when the camera was denied —
   *  DOM signals and heartbeats still run). `screen` is the entire-screen share
   *  granted on the setup screen, or null when it was declined or lost. */
  async start(
    session: ProctorSession,
    video: HTMLVideoElement | null,
    screen: MediaStream | null = null,
  ): Promise<void> {
    this.video = video;
    this.attachDomListeners();

    this.flushTimer = setInterval(
      () => void this.flush(),
      session.event_batch_interval_seconds * 1000,
    );
    this.heartbeatTimer = setInterval(
      () => void this.heartbeat(),
      session.heartbeat_interval_seconds * 1000,
    );

    if (screen) this.setScreenStream(screen);
    // `screen.isExtended` (Screen Details API, Chromium) needs no permission and
    // says only "more than one display" — which is exactly the observation. On
    // browsers without it, the check is silently absent rather than guessed at.
    if ("isExtended" in window.screen) {
      this.displayTimer = setInterval(() => {
        const extended = (window.screen as Screen & { isExtended?: boolean }).isExtended;
        this.signal("multiple_displays", extended === true, performance.now());
      }, DISPLAY_CHECK_MS);
    }

    if (video) {
      const stream = video.srcObject as MediaStream | null;
      stream?.getVideoTracks().forEach((track) => {
        track.addEventListener("ended", () => this.cameraStopped());
      });
      void this.uploadBaseline(session, video);
      void this.loadDetectors().then(() => {
        if (!this.stopped) {
          this.detectTimer = setInterval(() => this.detect(), DETECT_INTERVAL_MS);
        }
      });
    }
  }

  /** The camera could not start at all. An observation, not a locked exam. */
  cameraDenied(): void {
    this.enqueue({ type: "camera_denied", occurred_at: new Date().toISOString() });
  }

  /** The screen was not shared when the sitting began. Same shape as the camera:
   *  an observation the author weighs, never a locked exam. */
  screenShareDenied(): void {
    this.enqueue({ type: "screen_share_denied", occurred_at: new Date().toISOString() });
  }

  /** Attach (or re-attach, after a re-share) the entire-screen stream. The stream
   *  is only watched for ending — no frames are read, streamed or stored. */
  setScreenStream(stream: MediaStream): void {
    this.screenStream = stream;
    stream.getVideoTracks().forEach((track) => {
      track.addEventListener("ended", () => this.screenShareStopped());
    });
    this.onNotice(null);
    this.onScreenChange(true);
  }

  private screenShareStopped(): void {
    this.screenStream = null;
    this.enqueue({ type: "screen_share_stopped", occurred_at: new Date().toISOString() });
    // Equipment, so the sitter can fix it. Never a judgement.
    this.onNotice("Your screen is no longer shared. Re-share it — the exam continues.");
    this.onScreenChange(false);
  }

  /** Whether an entire-screen share is currently live. */
  screenActive(): boolean {
    return this.screenStream?.getVideoTracks().some((t) => t.readyState === "live") ?? false;
  }

  /** Close open episodes, send what remains, stop timers. The server writes the
   *  session_end bookend when the session actually closes. */
  async stop(): Promise<void> {
    if (this.stopped) return;
    this.stopped = true;
    for (const timer of [
      this.detectTimer,
      this.flushTimer,
      this.heartbeatTimer,
      this.displayTimer,
    ]) {
      if (timer) clearInterval(timer);
    }
    this.domCleanup.forEach((cleanup) => cleanup());
    this.domCleanup = [];

    const now = performance.now();
    for (const [type, tracker] of this.trackers) {
      if (tracker.activeSince !== null) this.closeEpisode(type, tracker, now);
      if (tracker.pending) this.emitEpisode(tracker);
    }
    await this.flush();
  }

  // ------------------------------------------------------------- detection

  private async loadDetectors(): Promise<void> {
    try {
      const vision = await import("@mediapipe/tasks-vision");
      const fileset = await vision.FilesetResolver.forVisionTasks(WASM_BASE);
      this.faceLandmarker = await vision.FaceLandmarker.createFromOptions(fileset, {
        baseOptions: { modelAssetPath: FACE_MODEL_URL },
        runningMode: "VIDEO",
        numFaces: 3,
      });
      try {
        this.objectDetector = await vision.ObjectDetector.createFromOptions(fileset, {
          baseOptions: { modelAssetPath: OBJECT_MODEL_URL },
          runningMode: "VIDEO",
          scoreThreshold: 0.6,
          maxResults: 3,
        });
      } catch {
        // The phone detector is the weakest signal in the set (D10); running
        // without it is a degradation, not a failure.
      }
    } catch {
      // No face model: camera and DOM signals still work, detection does not.
      // Say so plainly — a sitter is entitled to know what is running.
      this.onNotice("Automatic detection could not load. The camera stays on.");
    }
  }

  private detect(): void {
    const video = this.video;
    if (!video || video.readyState < 2 || !this.faceLandmarker) return;
    this.tick += 1;
    const now = performance.now();

    let faces = 0;
    try {
      const result = this.faceLandmarker.detectForVideo(video, now);
      faces = result.faceLandmarks?.length ?? 0;

      this.signal("no_face", faces === 0, now);
      this.signal("multiple_faces", faces >= 2, now, faces >= 2 ? 0.9 : undefined);
      this.signal(
        "head_pose_away",
        faces === 1 && this.headTurnedAway(result.faceLandmarks[0]),
        now,
        0.5,
      );
    } catch {
      return; // one bad frame is nothing; the next tick tries again
    }

    if (this.objectDetector && this.tick % OBJECT_EVERY_NTH_TICK === 0) {
      try {
        const objects = this.objectDetector.detectForVideo(video, now);
        const phone = (objects.detections ?? []).find((detection) =>
          detection.categories?.some(
            (category) =>
              category.categoryName === "cell phone" && category.score >= 0.6,
          ),
        );
        this.signal("phone_visible", Boolean(phone), now, phone ? 0.6 : undefined);
      } catch {
        // ignore: weakest detector, weakest claim on our attention
      }
    }
  }

  /** Coarse head pose from landmark geometry, deliberately generous: someone
   *  looking down at scratch paper must not trip this. Only a sustained, strong
   *  turn does — and it is a low-severity, low-weight observation regardless. */
  private headTurnedAway(landmarks: { x: number; y: number }[]): boolean {
    const nose = landmarks[1];
    const left = landmarks[234];
    const right = landmarks[454];
    const top = landmarks[10];
    const chin = landmarks[152];
    if (!nose || !left || !right || !top || !chin) return false;

    const width = right.x - left.x;
    const height = chin.y - top.y;
    if (Math.abs(width) < 0.01 || Math.abs(height) < 0.01) return false;

    const yaw = (nose.x - (left.x + right.x) / 2) / width;
    const pitch = (nose.y - top.y) / height; // ~0.55 facing forward
    return Math.abs(yaw) > 0.28 || pitch > 0.8 || pitch < 0.3;
  }

  // ------------------------------------------------- episodes and debouncing

  private tracker(type: string): Tracker {
    let tracker = this.trackers.get(type);
    if (!tracker) {
      tracker = {
        activeSince: null,
        inactiveSince: null,
        checkpointedAt: null,
        pending: null,
        emitted: [],
      };
      this.trackers.set(type, tracker);
    }
    return tracker;
  }

  /** Feed one boolean observation for one signal. All debouncing lives here. */
  private signal(type: ProctorEventType, active: boolean, now: number, confidence?: number): void {
    const spec = SIGNALS[type];
    if (!spec) return;
    const tracker = this.tracker(type);

    if (active) {
      tracker.inactiveSince = null;
      if (tracker.activeSince === null) {
        tracker.activeSince = now;
        tracker.checkpointedAt = null;
        tracker.confidence = confidence;
      }
      // A still is taken the moment the episode is confirmed — the condition is
      // on screen right now, which is exactly what the author needs to judge it.
      const runningFor = now - tracker.activeSince;
      if (spec.still && runningFor >= spec.minMs && !tracker.pending?.still && this.video) {
        const pendingRef = tracker;
        if (!pendingRef.pending || !pendingRef.pending.still) {
          void captureStill(this.video).then((still) => {
            if (still && pendingRef.activeSince !== null) {
              pendingRef.pending = pendingRef.pending ?? null;
              // stash on the tracker; attached to the episode when it closes
              (pendingRef as Tracker & { stagedStill?: Blob }).stagedStill = still;
            }
          });
        }
      }
      // Checkpoint a condition that never ends, so a wall-cam hour is visible
      // while it happens rather than only at submit.
      const sinceCheckpoint = now - (tracker.checkpointedAt ?? tracker.activeSince);
      if (sinceCheckpoint >= CHECKPOINT_MS) {
        this.closeEpisode(type, tracker, now);
        tracker.activeSince = now;
        tracker.checkpointedAt = now;
        if (tracker.pending) this.emitEpisode(tracker);
      }
      return;
    }

    if (tracker.activeSince !== null) {
      // Condition just went quiet: close only after the grace period, so one
      // dropped frame does not split an episode in two.
      if (tracker.inactiveSince === null) tracker.inactiveSince = now;
      if (now - tracker.inactiveSince >= spec.graceMs) {
        this.closeEpisode(type, tracker, now - (now - tracker.inactiveSince));
        tracker.activeSince = null;
        tracker.inactiveSince = null;
      }
      return;
    }

    // Fully quiet: flush a pending episode once the coalescing window has passed.
    if (tracker.pending && now - tracker.pending.endedAt >= spec.coalesceMs) {
      this.emitEpisode(tracker);
    }
  }

  /** An instantaneous observation (copy, paste, context menu). */
  private pulse(type: ProctorEventType): void {
    const now = performance.now();
    const tracker = this.tracker(type);
    if (tracker.pending && now - tracker.pending.endedAt < SIGNALS[type].coalesceMs) {
      tracker.pending.occurrences += 1;
      tracker.pending.endedAt = now;
      return;
    }
    if (tracker.pending) this.emitEpisode(tracker);
    tracker.pending = {
      type,
      startedAt: now,
      endedAt: now,
      durationMs: 0,
      occurrences: 1,
    };
  }

  private closeEpisode(type: string, tracker: Tracker, now: number): void {
    const spec = SIGNALS[type];
    const startedAt = tracker.activeSince;
    if (startedAt === null) return;
    const durationMs = Math.round(now - startedAt);
    if (durationMs < spec.minMs) return; // never sustained: not an episode

    const staged = (tracker as Tracker & { stagedStill?: Blob }).stagedStill;
    delete (tracker as Tracker & { stagedStill?: Blob }).stagedStill;

    if (tracker.pending && now - tracker.pending.endedAt < spec.coalesceMs) {
      tracker.pending.occurrences += 1;
      tracker.pending.durationMs += durationMs;
      tracker.pending.endedAt = now;
      tracker.pending.still = tracker.pending.still ?? staged;
      return;
    }
    if (tracker.pending) this.emitEpisode(tracker);
    tracker.pending = {
      type: type as ProctorEventType,
      startedAt,
      endedAt: now,
      durationMs,
      occurrences: 1,
      confidence: tracker.confidence,
      still: staged,
    };
  }

  private emitEpisode(tracker: Tracker): void {
    const episode = tracker.pending;
    tracker.pending = null;
    if (!episode) return;

    // The per-minute cap: a stuck detector must not flood the record. Dropped
    // episodes are dropped silently — the ones already emitted carry the story.
    const now = performance.now();
    tracker.emitted = tracker.emitted.filter((at) => now - at < 60_000);
    if (tracker.emitted.length >= SIGNALS[episode.type].maxPerMin) return;
    tracker.emitted.push(now);

    const ref = `e${++this.refCounter}`;
    if (episode.still) this.stills.set(ref, episode.still);
    this.enqueue({
      client_ref: ref,
      type: episode.type,
      occurred_at: new Date(Date.now() - (performance.now() - episode.startedAt)).toISOString(),
      duration_ms: episode.durationMs || undefined,
      occurrences: episode.occurrences,
      confidence: episode.confidence,
      has_still: Boolean(episode.still),
    });
  }

  private enqueue(event: Omit<ProctorEventIn, "client_ref"> & { client_ref?: string }): void {
    if (this.queue.length >= QUEUE_CAP) return;
    this.queue.push({ client_ref: event.client_ref ?? `e${++this.refCounter}`, ...event });
  }

  // ----------------------------------------------------------- DOM signals

  private attachDomListeners(): void {
    const on = (target: Document | Window, name: string, handler: () => void) => {
      target.addEventListener(name, handler);
      this.domCleanup.push(() => target.removeEventListener(name, handler));
    };

    on(document, "visibilitychange", () => {
      this.signal("tab_blur", document.visibilityState === "hidden", performance.now());
    });
    on(window, "blur", () => this.signal("window_blur", true, performance.now()));
    on(window, "focus", () => this.signal("window_blur", false, performance.now()));
    on(document, "copy", () => this.pulse("copy"));
    on(document, "paste", () => this.pulse("paste"));
    on(document, "contextmenu", () => this.pulse("context_menu"));
  }

  private cameraStopped(): void {
    this.enqueue({ type: "camera_stopped", occurred_at: new Date().toISOString() });
    // Technical status, not a judgement: equipment, so the sitter can fix it.
    this.onNotice("Your camera has stopped. Check it — the exam continues.");
  }

  private async uploadBaseline(session: ProctorSession, video: HTMLVideoElement): Promise<void> {
    if (!session.baseline_upload_url) return;
    for (let attempt = 0; attempt < 10; attempt++) {
      const still = await captureStill(video);
      if (still) {
        await uploadEvidence(session.baseline_upload_url, still);
        return;
      }
      await new Promise((resolve) => setTimeout(resolve, 500)); // camera warming up
    }
  }

  // ------------------------------------------------------------- transport

  private async flush(): Promise<void> {
    if (this.flushing || this.queue.length === 0) return;
    this.flushing = true;
    const batch = this.queue.splice(0, 50);
    try {
      const { results } = await recordProctorEvents(this.attemptId, batch);
      for (const ack of results) {
        if (!ack.client_ref) continue;
        const still = this.stills.get(ack.client_ref);
        this.stills.delete(ack.client_ref);
        if (still && ack.upload_url) void uploadEvidence(ack.upload_url, still);
      }
    } catch (caught) {
      if (caught instanceof ApiRequestError && caught.status === 409) {
        // The session has ended server-side; there is nothing left to report to.
        this.queue = [];
      } else {
        // Network hole: put the batch back and let the next tick retry. The
        // server records the silence itself either way.
        this.queue = [...batch, ...this.queue].slice(0, QUEUE_CAP);
      }
    } finally {
      this.flushing = false;
    }
  }

  private async heartbeat(): Promise<void> {
    try {
      await proctorHeartbeat(this.attemptId);
    } catch {
      // A missed heartbeat is the server's evidence, not this client's problem.
    }
  }
}

// Minimal shapes for the two MediaPipe results we read, so the dynamic import
// stays typed without pulling the library into the bundle graph statically.
type FaceResult = { faceLandmarks: { x: number; y: number }[][] };
type ObjectResult = {
  detections?: { categories?: { categoryName: string; score: number }[] }[];
};
