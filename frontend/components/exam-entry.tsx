"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";

import { GlassCard, Eyebrow } from "@/components/glass";
import { ProctorConsent } from "@/components/proctor-consent";
import { examPreview, startAttempt, type ExamPreview } from "@/lib/api/attempts";
import { ApiRequestError } from "@/lib/api/client";
import {
  stashProctorStreams,
  takeProctorStreams,
  type ProctorStreams,
} from "@/lib/proctoring/streams";

/**
 * What the link shows before somebody commits to sitting.
 *
 * Deliberately thin, and it matches what the server will send: the paper's name, its
 * shape, whether it is open. No questions, no author identity, no results — a preview
 * that leaked a stem would be a preview somebody screenshots and studies.
 *
 * A proctored paper interposes the consent screen between "Begin" and the attempt
 * actually starting: consent is read before the clock runs, and before any camera
 * exists. Declining the camera still proceeds — the denial becomes an observation
 * for the author, never a locked door.
 */
export function ExamEntry({ token }: { token: string }) {
  const router = useRouter();
  const [preview, setPreview] = useState<ExamPreview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [consenting, setConsenting] = useState(false);

  useEffect(() => {
    const timer = setTimeout(async () => {
      try {
        setPreview(await examPreview(token));
      } catch (caught) {
        setError(
          caught instanceof ApiRequestError ? caught.message : "That link is not valid.",
        );
      }
    }, 0);
    return () => clearTimeout(timer);
  }, [token]);

  async function begin(streams?: ProctorStreams) {
    // Consent and equipment come before the attempt exists, so the clock has not
    // started while somebody reads what the camera will do or hunts for a cable.
    if (preview?.proctoring_enabled && !consenting) {
      setConsenting(true);
      return;
    }
    setBusy(true);
    setError(null);
    // The streams granted on the setup screen ride a module stash into the
    // runner: a screen share cannot be re-asked without prompting again.
    if (streams) stashProctorStreams(streams);
    try {
      const attempt = await startAttempt(token);
      router.replace(`/attempt/${attempt.id}`);
    } catch (caught) {
      // Starting failed; release whatever hardware the setup screen granted.
      const orphaned = takeProctorStreams();
      orphaned?.camera?.getTracks().forEach((track) => track.stop());
      orphaned?.screen?.getTracks().forEach((track) => track.stop());
      setError(caught instanceof ApiRequestError ? caught.message : "Could not start.");
      setBusy(false);
      setConsenting(false);
    }
  }

  if (error && !preview) {
    return (
      <GlassCard raised className="rise w-full max-w-lg p-8">
        <Eyebrow>Kitaably</Eyebrow>
        <h1 className="mt-3 font-display text-2xl font-semibold">This link doesn&apos;t work</h1>
        <p role="alert" className="mt-3 text-sm leading-relaxed text-parchment-dim">{error}</p>
      </GlassCard>
    );
  }

  if (!preview) {
    return <p className="text-sm text-parchment-dim">Loading…</p>;
  }

  if (consenting) {
    return <ProctorConsent busy={busy} onReady={(streams) => void begin(streams)} />;
  }

  return (
    <GlassCard raised className="rise w-full max-w-lg p-8 sm:p-10">
      <Eyebrow>You have been sent a paper</Eyebrow>
      <h1 className="mt-3 font-display text-3xl font-semibold tracking-tight">
        {preview.title}
      </h1>

      <dl className="mt-6 grid grid-cols-2 gap-4 text-sm">
        <div>
          <dt className="font-mono text-[11px] font-medium tracking-[0.02em] text-parchment-dim">
            Questions
          </dt>
          <dd className="mt-1">{preview.question_count}</dd>
        </div>
        <div>
          <dt className="font-mono text-[11px] font-medium tracking-[0.02em] text-parchment-dim">
            Time
          </dt>
          <dd className="mt-1">
            {preview.duration_minutes ? `${preview.duration_minutes} minutes` : "No limit"}
          </dd>
        </div>
        {/* What kind of paper, before committing to sitting it. Derived server-side
            from the formats it was actually written in, so it cannot disagree with
            what is about to appear. Deliberately coarse: the exact format mix would
            start to describe the questions. */}
        <div className="col-span-2">
          <dt className="font-mono text-[11px] font-medium tracking-[0.02em] text-parchment-dim">
            Answered by
          </dt>
          <dd className="mt-1">
            {preview.type === "subjective"
              ? "Writing"
              : preview.type === "mcq"
                ? "Picking and arranging"
                : "Some picking, some writing"}
          </dd>
        </div>
      </dl>

      {preview.duration_minutes && (
        <p className="mt-5 text-xs leading-relaxed text-parchment-dim">
          The clock starts when you begin and does not pause. Answers save as you go, so
          closing the tab will not lose them — but it will not stop the clock either.
        </p>
      )}

      {preview.proctoring_enabled && (
        <p className="mt-5 rounded-lg border border-parchment/18 bg-parchment/5 px-3 py-2 text-xs leading-relaxed text-parchment-dim">
          This paper is proctored: you&apos;ll be asked to allow your camera, share
          your entire screen, and sit with a single display. A person reviews
          anything the sitting notes. What that means is spelled out on the next
          screen, before the clock starts.
        </p>
      )}

      {!preview.is_open && (
        <p role="alert" className="mt-5 rounded-lg border border-saffron/40 bg-saffron/10 px-3 py-2 text-sm text-saffron">
          This paper is not open right now.
        </p>
      )}

      {error && (
        <p role="alert" className="mt-5 rounded-lg border border-danger/40 bg-danger/10 px-3 py-2 text-sm text-danger">
          {error}
        </p>
      )}

      <button
        onClick={() => void begin()}
        disabled={busy || !preview.is_open}
        className="mt-7 w-full rounded-xl bg-indigo px-4 py-3 text-sm font-medium transition hover:bg-indigo/85 disabled:opacity-50"
      >
        {busy
          ? "Opening…"
          : preview.already_started
            ? "Resume"
            : "Begin"}
      </button>

      {preview.already_started && (
        <p className="mt-3 text-center text-xs text-parchment-dim">
          You have already started this one. Resuming does not give you more time.
        </p>
      )}
    </GlassCard>
  );
}
