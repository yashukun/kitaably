/**
 * Hands the equipment streams from the setup screen to the exam runner.
 *
 * A screen share can only be asked for once per user gesture — `getDisplayMedia`
 * always prompts — so the stream granted on the setup screen must survive the
 * client-side navigation into the runner rather than being stopped and re-asked.
 * A module singleton survives exactly that (App Router navigation keeps the JS
 * context) and nothing more: a hard reload loses it, which the runner treats as
 * "not shared" and offers a re-share button.
 *
 * `take` transfers ownership. Whoever takes the streams stops their tracks.
 */

export type ProctorStreams = {
  camera: MediaStream | null;
  screen: MediaStream | null;
  /** The sitter explicitly declined on the setup screen (as opposed to the
   *  stream simply not surviving a reload). Recorded once as an observation. */
  cameraDeclined: boolean;
  screenDeclined: boolean;
};

let stash: ProctorStreams | null = null;

export function stashProctorStreams(streams: ProctorStreams): void {
  // Anything already stashed is orphaned — release the hardware.
  if (stash) {
    stash.camera?.getTracks().forEach((track) => track.stop());
    stash.screen?.getTracks().forEach((track) => track.stop());
  }
  stash = streams;
}

export function takeProctorStreams(): ProctorStreams | null {
  const taken = stash;
  stash = null;
  return taken;
}
