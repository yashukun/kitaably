"use client";

import { useEffect, useRef, useState } from "react";

import { GlassCard, Eyebrow } from "@/components/glass";
import type { ProctorStreams } from "@/lib/proctoring/streams";

/**
 * The consent-and-setup screen. A real screen with real content, not a checkbox:
 * a sitter is entitled to know that a camera and their screen will be watched,
 * what is kept, for how long, who sees it, and that a human — not a model —
 * decides what any of it means.
 *
 * Three checks, in the order a person can fix them: allow the camera, share the
 * entire screen, disconnect any extra display. The screen share granted here is
 * handed to the runner alive (getDisplayMedia always prompts, so stopping and
 * re-asking would prompt twice for one consent).
 *
 * Declining any of it is a first-class path. A denial degrades to an observation
 * the author weighs, never a locked exam — a person with a broken webcam must
 * still be able to answer the paper. The extra-display check asks and re-checks,
 * and can also be walked past; walking past it is simply noted.
 */

type CheckState = "pending" | "granted" | "denied" | "wrong-surface" | "skipped";

function CheckRow({
  label,
  state,
  children,
}: {
  label: string;
  state: "ok" | "todo" | "warn";
  children: React.ReactNode;
}) {
  const dot =
    state === "ok" ? "bg-canon" : state === "warn" ? "bg-saffron" : "bg-parchment-dim/40";
  return (
    <li className="flex items-start gap-3 rounded-xl border border-parchment/12 px-4 py-3">
      <span aria-hidden className={`mt-1.5 inline-block h-2 w-2 shrink-0 rounded-full ${dot}`} />
      <div className="min-w-0 flex-1">
        <p className="text-sm text-parchment">{label}</p>
        <div className="mt-1 text-xs leading-relaxed text-parchment-dim">{children}</div>
      </div>
    </li>
  );
}

export function ProctorConsent({
  onReady,
  busy,
}: {
  /** Called once the sitter proceeds, carrying whatever equipment they granted.
   *  The caller takes ownership of the streams. */
  onReady: (streams: ProctorStreams) => void;
  busy: boolean;
}) {
  const [camera, setCamera] = useState<CheckState>("pending");
  const [screenShare, setScreenShare] = useState<CheckState>("pending");
  // null: the browser cannot say (no Screen Details API) — not treated as a fault.
  const [extended, setExtended] = useState<boolean | null>(null);
  const [asking, setAsking] = useState(false);

  const cameraStream = useRef<MediaStream | null>(null);
  const screenStream = useRef<MediaStream | null>(null);
  const handedOff = useRef(false);

  // The display check runs by itself: unplugging a monitor should turn the row
  // green without anybody hunting for a re-check button.
  useEffect(() => {
    if (!("isExtended" in window.screen)) return;
    const read = () =>
      setExtended(
        (window.screen as Screen & { isExtended?: boolean }).isExtended === true,
      );
    read();
    const timer = setInterval(read, 2000);
    return () => clearInterval(timer);
  }, []);

  // Whoever begins takes the streams; anyone who navigates away releases them.
  useEffect(() => {
    return () => {
      if (handedOff.current) return;
      cameraStream.current?.getTracks().forEach((track) => track.stop());
      screenStream.current?.getTracks().forEach((track) => track.stop());
    };
  }, []);

  async function allowCamera() {
    setAsking(true);
    try {
      cameraStream.current = await navigator.mediaDevices.getUserMedia({
        video: { width: { ideal: 640 }, facingMode: "user" },
        audio: false,
      });
      setCamera("granted");
    } catch {
      setCamera("denied");
    } finally {
      setAsking(false);
    }
  }

  async function shareScreen() {
    setAsking(true);
    try {
      const stream = await navigator.mediaDevices.getDisplayMedia({
        // A hint the browser may honour; the settings check below is the test.
        video: { displaySurface: "monitor" } as MediaTrackConstraints,
        audio: false,
      });
      const surface = (
        stream.getVideoTracks()[0]?.getSettings() as MediaTrackSettings & {
          displaySurface?: string;
        }
      )?.displaySurface;
      // A window or tab share leaves the rest of the screen unobserved, which is
      // not what was consented to. Ask again rather than quietly accepting less.
      if (surface && surface !== "monitor") {
        stream.getTracks().forEach((track) => track.stop());
        setScreenShare("wrong-surface");
        return;
      }
      screenStream.current = stream;
      setScreenShare("granted");
    } catch {
      setScreenShare("denied");
    } finally {
      setAsking(false);
    }
  }

  function begin() {
    handedOff.current = true;
    onReady({
      camera: cameraStream.current,
      screen: screenStream.current,
      cameraDeclined: camera !== "granted",
      screenDeclined: screenShare !== "granted",
    });
  }

  const cameraOk = camera === "granted";
  const screenOk = screenShare === "granted";
  const displaysOk = extended !== true;
  const allClear = cameraOk && screenOk && displaysOk;
  const somethingMissing = !cameraOk || !screenOk;

  return (
    <GlassCard raised className="rise w-full max-w-lg p-8 sm:p-10">
      <Eyebrow>Before you begin</Eyebrow>
      <h1 className="mt-3 font-display text-2xl font-semibold tracking-tight">
        This paper is proctored
      </h1>

      <ul className="mt-5 flex flex-col gap-2.5 text-sm leading-relaxed text-parchment-dim">
        <li>
          <span className="text-parchment">Your camera stays on while you sit.</span>{" "}
          Detection runs in your browser. No video is streamed or recorded — what
          leaves this page is a log of moments (for example,{" "}
          <em>&ldquo;no face detected for 42s&rdquo;</em>) and, for a few of them, a
          single small snapshot.
        </li>
        <li>
          <span className="text-parchment">Your screen share is watched, not recorded.</span>{" "}
          Sharing your entire screen lets the sitting note when the share stops.
          No screen images ever leave your browser.
        </li>
        <li>
          <span className="text-parchment">Activity in this tab is noted.</span>{" "}
          Switching tabs, pasting, extra displays, and similar moments are logged
          with times.
        </li>
        <li>
          <span className="text-parchment">Only the paper&rsquo;s author sees any of it,</span>{" "}
          and a person — not a model — reviews the log before anything is concluded
          from it. You see nothing mid-exam because nothing has been decided mid-exam.
        </li>
        <li>
          <span className="text-parchment">Snapshots are kept for 60 days</span> after
          your sitting closes, then deleted.
        </li>
      </ul>

      {/* ------------------------------------------------------ the checklist */}
      <ol className="mt-6 flex flex-col gap-2.5">
        <CheckRow label="Camera" state={cameraOk ? "ok" : camera === "denied" ? "warn" : "todo"}>
          {cameraOk ? (
            "Allowed."
          ) : camera === "denied" ? (
            "The camera was not available. You can still sit the paper — the author will see that it ran without one."
          ) : (
            <button
              type="button"
              onClick={() => void allowCamera()}
              disabled={asking || busy}
              className="rounded-lg border border-parchment/25 px-3 py-1.5 text-xs text-parchment transition hover:border-parchment/50 disabled:opacity-50"
            >
              Allow the camera
            </button>
          )}
        </CheckRow>

        <CheckRow
          label="Screen"
          state={screenOk ? "ok" : screenShare === "pending" ? "todo" : "warn"}
        >
          {screenOk ? (
            "Your entire screen is shared."
          ) : (
            <>
              {screenShare === "wrong-surface" && (
                <p className="mb-2 text-saffron">
                  That was a window or a tab. Choose{" "}
                  <span className="text-parchment">your entire screen</span> in the
                  picker, then try again.
                </p>
              )}
              {screenShare === "denied" && (
                <p className="mb-2">
                  The screen was not shared. You can still sit the paper — the author
                  will see that it ran without a screen share.
                </p>
              )}
              <button
                type="button"
                onClick={() => void shareScreen()}
                disabled={asking || busy}
                className="rounded-lg border border-parchment/25 px-3 py-1.5 text-xs text-parchment transition hover:border-parchment/50 disabled:opacity-50"
              >
                {screenShare === "pending" ? "Share your entire screen" : "Try again"}
              </button>
            </>
          )}
        </CheckRow>

        <CheckRow
          label="One display"
          state={displaysOk ? "ok" : "warn"}
        >
          {extended === null
            ? "Checked where the browser can say; yours cannot, and that is fine."
            : displaysOk
              ? "Only one display is connected."
              : "More than one display is connected. Disconnect or unplug the extra one — this re-checks by itself."}
        </CheckRow>
      </ol>

      {/* --------------------------------------------------------- proceed */}
      <div className="mt-7 flex flex-col gap-3">
        <button
          onClick={begin}
          disabled={busy || asking || !allClear}
          className="w-full rounded-xl bg-indigo px-4 py-3 text-sm font-medium transition hover:bg-indigo/85 disabled:opacity-50"
        >
          {busy ? "Opening…" : "Begin"}
        </button>
        {!allClear && (
          <button
            onClick={begin}
            disabled={busy || asking}
            className="w-full rounded-xl border border-parchment/18 px-4 py-3 text-sm text-parchment-dim transition hover:text-parchment disabled:opacity-50"
          >
            {somethingMissing
              ? "Begin anyway — what's missing will be noted for the author"
              : "Begin anyway — the extra display will be noted for the author"}
          </button>
        )}
      </div>
    </GlassCard>
  );
}
