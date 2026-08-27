"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";

import { GlassCard, Eyebrow } from "@/components/glass";
import { QuestionInput, QuestionStem, isAnswered } from "@/components/question-input";
import { getAttempt, saveAnswer, submitAttempt, type Attempt } from "@/lib/api/attempts";
import { ApiRequestError } from "@/lib/api/client";
import { openProctorSession } from "@/lib/api/proctoring";
import { ProctorMonitor } from "@/lib/proctoring/monitor";
import { takeProctorStreams } from "@/lib/proctoring/streams";
import { formatLabel } from "@/lib/formats";

/**
 * The exam runner.
 *
 * Two things it must get right, and one it must not pretend to.
 *
 * **Autosave.** Every keystroke is not a request; every answer left unsaved is a lost
 * mark. Answers are debounced per question and flushed on submit, and the save state
 * is shown rather than assumed.
 *
 * **The clock.** The countdown here is cosmetic. `deadline_at` was fixed server-side
 * when the attempt started, and the server refuses a save after it whatever this
 * component believes — so a paused JS timer, a slow tab or a rewound system clock
 * cannot buy time. When the clock runs out this submits, because a sitter whose
 * browser froze should still have their answers counted.
 *
 * **Fourteen formats, one autosave.** Every question type — a match grid, a
 * select-all, an ordering — is answered into the same `response` string, structured
 * ones as JSON. So the debounce, the flush-on-submit and the deadline all behave
 * identically whatever the paper is made of, and adding a format cannot quietly
 * introduce a fifteenth way to lose somebody's answer.
 *
 * **Proctoring, when the paper asks for it.** Detection runs in this browser
 * (lib/proctoring/monitor.ts); this component only opens the session, hands the
 * monitor a self-view video element, and stops it on submit. What the sitter
 * sees here is that proctoring is *active* — the indicator and their own
 * camera — and nothing evaluative, because nothing has been decided yet: no
 * score, no warnings, no live suspicion meter. The one exception is a neutral
 * equipment notice ("your camera has stopped"), which is about hardware the
 * sitter can fix, never about conduct. Failure of any part of the camera path
 * degrades to an observation the author weighs — it must never block answering.
 */

const AUTOSAVE_MS = 800;

type SaveState = "idle" | "saving" | "saved" | "failed";

function remaining(deadline: string | null): number | null {
  if (!deadline) return null;
  return Math.max(0, new Date(deadline).getTime() - Date.now());
}

function formatClock(ms: number): string {
  const total = Math.floor(ms / 1000);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const pad = (n: number) => String(n).padStart(2, "0");
  return h > 0 ? `${h}:${pad(m)}:${pad(s)}` : `${pad(m)}:${pad(s)}`;
}

export function ExamRunner({ attemptId }: { attemptId: string }) {
  const router = useRouter();
  const [attempt, setAttempt] = useState<Attempt | null>(null);
  const [responses, setResponses] = useState<Record<string, string>>({});
  const [saveState, setSaveState] = useState<SaveState>("idle");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [left, setLeft] = useState<number | null>(null);
  // Handing in with blanks is allowed but never accidental: the first press asks,
  // inline, and the second one hands in. A browser dialog would block the page.
  const [confirmingSubmit, setConfirmingSubmit] = useState(false);
  // A copy of the paper was refused and told the sitter so, briefly.
  const [copyTried, setCopyTried] = useState(false);
  const copyNoticeTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Pending debounce timers, one per question, so typing in question 3 does not
  // cancel the save for question 1.
  const timers = useRef<Record<string, ReturnType<typeof setTimeout>>>({});
  // Latest value per question, read by flush() — state would be stale inside a timer.
  const latest = useRef<Record<string, string>>({});
  const submitted = useRef(false);

  // Proctoring. The monitor owns detection and batching; this component owns the
  // camera and screen streams and the self-view element the monitor reads frames
  // from. Streams granted on the setup screen arrive through the module stash —
  // a screen share cannot be re-asked without prompting again.
  const monitor = useRef<ProctorMonitor | null>(null);
  const cameraStream = useRef<MediaStream | null>(null);
  const screenStream = useRef<MediaStream | null>(null);
  const selfView = useRef<HTMLVideoElement | null>(null);
  const [proctorState, setProctorState] = useState<"off" | "camera" | "no-camera">("off");
  // Whether an entire-screen share is currently live. Equipment state only; the
  // header shows a re-share button while it is false.
  const [screenShared, setScreenShared] = useState(false);
  // Neutral, technical notices only — equipment, never conduct.
  const [proctorNotice, setProctorNotice] = useState<string | null>(null);

  useEffect(() => {
    const timer = setTimeout(async () => {
      try {
        const loaded = await getAttempt(attemptId);
        setAttempt(loaded);
        const initial: Record<string, string> = {};
        for (const answer of loaded.answers) {
          if (answer.response !== null) initial[answer.question_id] = answer.response;
        }
        setResponses(initial);
        latest.current = { ...initial };
        if (loaded.status !== "in_progress") router.replace(`/attempt/${attemptId}/result`);
      } catch (caught) {
        setError(caught instanceof ApiRequestError ? caught.message : "Could not load the paper.");
      }
    }, 0);
    return () => clearTimeout(timer);
  }, [attemptId, router]);

  // Open the proctor session and start the monitor once the paper is loaded.
  // Every failure path here degrades: no camera, no models, no session — the
  // paper stays answerable, and silence is the server's evidence to record.
  useEffect(() => {
    if (!attempt?.proctoring_enabled || attempt.status !== "in_progress") return;
    if (monitor.current) return;
    let cancelled = false;

    // Whatever the setup screen granted. Taken exactly once; absent on a hard
    // reload, which each path below degrades through on its own.
    const granted = takeProctorStreams();

    (async () => {
      try {
        const session = await openProctorSession(attempt.id);
        if (cancelled) return;
        const started = new ProctorMonitor(attempt.id, setProctorNotice, setScreenShared);
        monitor.current = started;

        // --- camera: the granted stream, or a silent re-acquire after a reload.
        let video: HTMLVideoElement | null = null;
        let camera = granted?.camera ?? null;
        if (!camera && !granted?.cameraDeclined) {
          // No stream survived (hard reload). The permission did, so this is
          // silent; asking again after an explicit decline would be hostile.
          try {
            camera = await navigator.mediaDevices.getUserMedia({
              video: { width: { ideal: 640 }, facingMode: "user" },
              audio: false,
            });
          } catch {
            camera = null;
          }
        }
        if (cancelled) {
          camera?.getTracks().forEach((track) => track.stop());
          granted?.screen?.getTracks().forEach((track) => track.stop());
          return;
        }
        if (camera) {
          cameraStream.current = camera;
          video = selfView.current;
          if (video) {
            video.srcObject = camera;
            await video.play().catch(() => {});
          }
          setProctorState("camera");
        } else {
          // Denied or unavailable: an observation for the author, not a wall.
          setProctorState("no-camera");
          started.cameraDenied();
        }

        // --- screen: only the setup screen's grant can exist here (a share
        // never survives a reload, and re-asking needs a user gesture — the
        // header offers a button). Absence is recorded either way.
        const screen = granted?.screen ?? null;
        if (screen && screen.getVideoTracks().some((t) => t.readyState === "live")) {
          screenStream.current = screen;
          setScreenShared(true);
        } else {
          started.screenShareDenied();
          setScreenShared(false);
        }
        await started.start(session, video, screenStream.current);
      } catch {
        // The session could not open (already ended, or a server hiccup). The
        // exam continues; the server records the resulting silence itself.
        granted?.screen?.getTracks().forEach((track) => track.stop());
      }
    })();

    return () => {
      cancelled = true;
      void monitor.current?.stop();
      monitor.current = null;
      cameraStream.current?.getTracks().forEach((track) => track.stop());
      cameraStream.current = null;
      screenStream.current?.getTracks().forEach((track) => track.stop());
      screenStream.current = null;
    };
  }, [attempt]);

  /** Mid-exam re-share, after the share stopped or a reload lost it. Needs a
   *  user gesture, hence a button rather than an automatic prompt. */
  const reshareScreen = useCallback(async () => {
    try {
      const stream = await navigator.mediaDevices.getDisplayMedia({
        video: { displaySurface: "monitor" } as MediaTrackConstraints,
        audio: false,
      });
      const surface = (
        stream.getVideoTracks()[0]?.getSettings() as MediaTrackSettings & {
          displaySurface?: string;
        }
      )?.displaySurface;
      if (surface && surface !== "monitor") {
        stream.getTracks().forEach((track) => track.stop());
        setProctorNotice(
          "That was a window or a tab. Share your entire screen — the exam continues.",
        );
        return;
      }
      screenStream.current?.getTracks().forEach((track) => track.stop());
      screenStream.current = stream;
      monitor.current?.setScreenStream(stream);
      setScreenShared(true);
    } catch {
      // Declined again. The share's absence is already the recorded observation.
    }
  }, []);

  const persist = useCallback(
    async (questionId: string, value: string) => {
      setSaveState("saving");
      try {
        await saveAnswer(attemptId, questionId, value === "" ? null : value);
        setSaveState("saved");
      } catch (caught) {
        setSaveState("failed");
        // A refused save is usually the deadline. Say which, rather than "failed".
        setError(
          caught instanceof ApiRequestError ? caught.message : "That answer did not save.",
        );
      }
    },
    [attemptId],
  );

  function change(questionId: string, value: string) {
    // Answering more invalidates the "still blank" question the confirm asked.
    setConfirmingSubmit(false);
    setResponses((current) => ({ ...current, [questionId]: value }));
    latest.current[questionId] = value;
    clearTimeout(timers.current[questionId]);
    timers.current[questionId] = setTimeout(() => void persist(questionId, value), AUTOSAVE_MS);
  }

  const submit = useCallback(
    async (automatic = false) => {
      if (submitted.current) return;
      submitted.current = true;
      setBusy(true);
      setError(null);

      // Flush every pending debounce before handing in, or the last thing typed is
      // the one answer that does not count.
      Object.values(timers.current).forEach(clearTimeout);
      try {
        await Promise.all(
          Object.entries(latest.current).map(([questionId, value]) =>
            saveAnswer(attemptId, questionId, value === "" ? null : value),
          ),
        );
      } catch {
        // The deadline may have passed mid-flush. Submit anyway: what did save counts,
        // and refusing to submit would leave the attempt open with nothing gained.
      }

      // Observation ends with the sitting: flush the monitor's last batch before
      // the submit closes the session server-side, then release the camera.
      try {
        await monitor.current?.stop();
      } catch {
        // A lost final batch is a gap in evidence, not a reason to hold the paper.
      }
      cameraStream.current?.getTracks().forEach((track) => track.stop());
      screenStream.current?.getTracks().forEach((track) => track.stop());

      try {
        await submitAttempt(attemptId);
        router.replace(`/attempt/${attemptId}/result`);
      } catch (caught) {
        submitted.current = false;
        setBusy(false);
        if (!automatic) {
          setError(caught instanceof ApiRequestError ? caught.message : "Could not submit.");
        }
      }
    },
    [attemptId, router],
  );

  /** Refuse copying or dragging the paper's content out of the sitting.
   *
   *  The mirror of the paste block on typed answers (question-input.tsx): answers
   *  are typed here, and the questions stay here. A UX rule, not a security
   *  boundary — the client is untrusted, a photograph of the screen defeats it,
   *  and during a proctored sitting the attempt is still recorded as an
   *  observation, because the document-level `copy` listener fires before the
   *  default is prevented. The typed answer fields refuse their own copy/cut
   *  already; this catches the stems and options around them. */
  const refuseCopy = useCallback(
    (event: { preventDefault: () => void; defaultPrevented: boolean }) => {
      // A typed answer field already refused this one and explained itself.
      if (event.defaultPrevented) return;
      event.preventDefault();
      setCopyTried(true);
      if (copyNoticeTimer.current) clearTimeout(copyNoticeTimer.current);
      copyNoticeTimer.current = setTimeout(() => setCopyTried(false), 4000);
    },
    [],
  );

  // The countdown. Cosmetic — see the note at the top of this file.
  useEffect(() => {
    if (!attempt?.deadline_at) return;
    const tick = () => {
      const ms = remaining(attempt.deadline_at);
      setLeft(ms);
      if (ms !== null && ms <= 0) void submit(true);
    };
    tick();
    const timer = setInterval(tick, 1000);
    return () => clearInterval(timer);
  }, [attempt?.deadline_at, submit]);

  if (error && !attempt) {
    return (
      <GlassCard className="mx-auto mt-16 max-w-lg p-7">
        <p role="alert" className="text-sm text-danger">{error}</p>
      </GlassCard>
    );
  }
  if (!attempt) {
    return <p className="mt-16 text-center text-sm text-parchment-dim">Loading the paper…</p>;
  }

  const answered = attempt.questions.filter((q) =>
    isAnswered(q, responses[q.id] ?? ""),
  ).length;
  const urgent = left !== null && left < 60_000;

  return (
    <div className="mx-auto w-full max-w-3xl px-5 py-8 sm:px-8">
      <header className="sticky top-0 z-10 -mx-5 mb-8 border-b border-parchment/10 bg-ink/80 px-5 py-4 backdrop-blur-xl sm:-mx-8 sm:px-8">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="min-w-0">
            <Eyebrow>Sitting</Eyebrow>
            <h1 className="mt-1 truncate font-display text-xl font-semibold">{attempt.title}</h1>
          </div>
          <div className="flex items-center gap-4">
            {/* The sitter is entitled to see that the camera is on — their own
                feed and a plain label. Nothing evaluative: no score, no meter,
                no colour that shifts with what the detector thinks. */}
            {attempt.proctoring_enabled && (
              <span className="flex items-center gap-2" aria-label="Proctoring is active">
                <video
                  ref={selfView}
                  muted
                  playsInline
                  className={`h-10 w-14 rounded-md border border-parchment/18 object-cover ${
                    proctorState === "camera" ? "" : "hidden"
                  }`}
                />
                <span className="flex items-center gap-1.5 font-mono text-[11px] text-parchment-dim">
                  <span
                    aria-hidden
                    className={`inline-block h-1.5 w-1.5 rounded-full ${
                      proctorState === "camera" ? "bg-danger" : "bg-parchment-dim/50"
                    }`}
                  />
                  {proctorState === "no-camera" ? "proctored · no camera" : "proctored"}
                </span>
                {/* Equipment the sitter can fix, so it gets a button, not a
                    warning. Nothing evaluative — the state is "shared or not". */}
                {!screenShared && (
                  <button
                    type="button"
                    onClick={() => void reshareScreen()}
                    className="rounded-lg border border-saffron/45 bg-saffron/10 px-2.5 py-1 font-mono text-[11px] text-saffron transition hover:bg-saffron/20"
                  >
                    share screen
                  </button>
                )}
              </span>
            )}
            <span className="font-mono text-[11px] text-parchment-dim">
              {answered}/{attempt.questions.length} answered
            </span>
            <span
              className="font-mono text-[11px] text-parchment-dim"
              aria-live="polite"
              aria-label={`Answers ${saveState}`}
            >
              {saveState === "saving" && "saving…"}
              {saveState === "saved" && "saved"}
              {saveState === "failed" && <span className="text-danger">not saved</span>}
            </span>
            {left !== null && (
              <span
                role="timer"
                className={`rounded-lg border px-2.5 py-1 font-mono text-sm ${
                  urgent
                    ? "border-danger/50 bg-danger/10 text-danger"
                    : "border-parchment/18 text-parchment"
                }`}
              >
                {formatClock(left)}
              </span>
            )}
          </div>
        </div>
      </header>

      {error && (
        <p role="alert" className="mb-6 rounded-xl border border-danger/40 bg-danger/10 px-4 py-3 text-sm text-danger">
          {error}
        </p>
      )}

      {/* Equipment status only — something the sitter can act on. Never a
          judgement, a warning about conduct, or anything a detector "thinks". */}
      {proctorNotice && (
        <p role="status" className="mb-6 rounded-xl border border-saffron/40 bg-saffron/10 px-4 py-3 text-sm text-saffron">
          {proctorNotice}
        </p>
      )}

      {/* One button per question: where you are, what is still blank, and a way
          to jump. Answered state comes from the same rule the counter uses. */}
      {attempt.questions.length > 3 && (
        <nav aria-label="Jump to a question" className="mb-6 flex flex-wrap gap-1.5">
          {attempt.questions.map((question) => {
            const done = isAnswered(question, responses[question.id] ?? "");
            return (
              <button
                key={question.id}
                type="button"
                aria-label={`Question ${question.index + 1}${done ? ", answered" : ", not answered"}`}
                onClick={() =>
                  document
                    .getElementById(`question-${question.index}`)
                    ?.scrollIntoView({ behavior: "smooth", block: "start" })
                }
                className={`h-8 w-8 rounded-lg border font-mono text-[11px] transition ${
                  done
                    ? "border-indigo/60 bg-indigo/15 text-parchment"
                    : "border-parchment/15 text-parchment-dim hover:border-parchment/35"
                }`}
              >
                {question.index + 1}
              </button>
            );
          })}
        </nav>
      )}

      {copyTried && (
        <p role="status" className="mb-4 font-mono text-[11px] text-saffron">
          Copying the paper is turned off during a sitting — the questions stay here,
          and answers are typed.
        </p>
      )}

      <ol
        className="flex flex-col gap-5"
        onCopy={refuseCopy}
        onCut={refuseCopy}
        onDragStart={refuseCopy}
      >
        {attempt.questions.map((question) => (
          <li key={question.id} id={`question-${question.index}`} className="scroll-mt-28">
            <GlassCard className="p-6">
              <div className="flex flex-wrap items-baseline justify-between gap-4">
                <Eyebrow>
                  Question {question.index + 1} · {formatLabel(question.format)}
                  {question.difficulty ? ` · ${question.difficulty}` : ""}
                </Eyebrow>
                <span className="font-mono text-[11px] text-parchment-dim">
                  {question.points} {question.points === 1 ? "mark" : "marks"}
                </span>
              </div>

              <QuestionStem question={question} />
              <QuestionInput
                question={question}
                value={responses[question.id] ?? ""}
                onChange={(value) => change(question.id, value)}
              />
            </GlassCard>
          </li>
        ))}
      </ol>

      <div className="mt-8 flex flex-wrap items-center justify-between gap-4">
        <p className="text-xs text-parchment-dim">
          {confirmingSubmit && answered < attempt.questions.length ? (
            <span className="text-saffron">
              {attempt.questions.length - answered}{" "}
              {attempt.questions.length - answered === 1 ? "question is" : "questions are"}{" "}
              still blank. Hand in anyway?
            </span>
          ) : (
            "Answers save as you go. Handing in is final."
          )}
        </p>
        <div className="flex items-center gap-3">
          {confirmingSubmit && !busy && (
            <button
              onClick={() => setConfirmingSubmit(false)}
              className="rounded-xl border border-parchment/18 px-4 py-2.5 text-sm text-parchment-dim transition hover:text-parchment"
            >
              Keep going
            </button>
          )}
          <button
            onClick={() => {
              if (answered < attempt.questions.length && !confirmingSubmit) {
                setConfirmingSubmit(true);
                return;
              }
              void submit();
            }}
            disabled={busy}
            className="rounded-xl bg-indigo px-6 py-2.5 text-sm font-medium transition hover:bg-indigo/85 disabled:opacity-50"
          >
            {busy ? "Handing in…" : confirmingSubmit ? "Hand in anyway" : "Hand in"}
          </button>
        </div>
      </div>
    </div>
  );
}
