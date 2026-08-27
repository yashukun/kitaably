"use client";

import { useMemo, useSyncExternalStore } from "react";

import type { ChatSession } from "@/lib/api/chat";

/**
 * Conversations, stacked down the left.
 *
 * This replaced a `<select>`, and the reason is not decoration. A dropdown shows one
 * conversation at a time — the one you are already in — so the rest of the transcript
 * history is invisible until you go looking for it, and a thing you have to remember
 * to open is a thing you stop opening. Chat history is navigation. Navigation belongs
 * on the page.
 *
 * Grouped by recency rather than listed flat, because a title alone does not tell you
 * whether you wrote it this morning or last month, and "which one was I just in" is
 * the question this list actually gets asked.
 */

const DAY = 86_400_000;

// A single slow clock, shared by every instance of this component.
//
// The headings below depend on "now", which is a browser fact: this component is
// server-rendered first, so reading the clock during render would bake the server's
// idea of "Today" into the HTML and the browser's into hydration, and React would
// throw the markup away over a difference neither of them is wrong about.
//
// `useSyncExternalStore` is the supported way out. The server snapshot is null — so
// SSR and the first client render agree — and the client snapshot is a cached number
// that only changes when the interval below moves it, which is what the hook requires
// (a snapshot that returned a fresh `Date.now()` on every call would re-render
// forever). It also means no `setState` inside an effect.
const CLOCK_TICK_MS = 300_000;

let clockNow = 0;
let clockTimer: ReturnType<typeof setInterval> | null = null;
const clockListeners = new Set<() => void>();

function subscribeToClock(onChange: () => void): () => void {
  if (clockListeners.size === 0) {
    clockNow = Date.now();
    // Slow on purpose. The only thing that moves is a heading at midnight; polling
    // faster would re-render the whole list to change nothing.
    clockTimer = setInterval(() => {
      clockNow = Date.now();
      for (const listener of clockListeners) listener();
    }, CLOCK_TICK_MS);
  }
  clockListeners.add(onChange);

  return () => {
    clockListeners.delete(onChange);
    if (clockListeners.size === 0 && clockTimer !== null) {
      clearInterval(clockTimer);
      clockTimer = null;
    }
  };
}

function readClock(): number {
  if (clockNow === 0) clockNow = Date.now();
  return clockNow;
}

/** No clock on the server: render flat, group once the browser takes over. */
const readClockOnServer = (): number | null => null;

type Group = { label: string; sessions: ChatSession[] };

function groupByRecency(sessions: ChatSession[], now: number): Group[] {
  // Boundaries are calendar days, not rolling 24-hour windows: something written at
  // 11pm should say "Yesterday" the next morning, not "Today" until 11pm.
  const startOfToday = new Date(now).setHours(0, 0, 0, 0);

  const buckets: Group[] = [
    { label: "Today", sessions: [] },
    { label: "Yesterday", sessions: [] },
    { label: "Previous 7 days", sessions: [] },
    { label: "Older", sessions: [] },
  ];

  for (const session of sessions) {
    const at = new Date(session.last_message_at ?? session.created_at).getTime();
    if (Number.isNaN(at)) {
      buckets[3].sessions.push(session);
    } else if (at >= startOfToday) {
      buckets[0].sessions.push(session);
    } else if (at >= startOfToday - DAY) {
      buckets[1].sessions.push(session);
    } else if (at >= startOfToday - 7 * DAY) {
      buckets[2].sessions.push(session);
    } else {
      buckets[3].sessions.push(session);
    }
  }

  return buckets.filter((bucket) => bucket.sessions.length > 0);
}

export function ChatSidebar({
  sessions,
  activeId,
  onSelect,
  onNew,
  busy,
}: {
  sessions: ChatSession[];
  activeId: string | null;
  onSelect: (id: string) => void;
  onNew: () => void;
  busy: boolean;
}) {
  const now = useSyncExternalStore(subscribeToClock, readClock, readClockOnServer);

  // `sessions` is re-created by the parent after every answer, so the grouping is
  // memoised on its identity rather than recomputed on each keystroke in the input.
  const groups = useMemo(
    () => (now === null ? null : groupByRecency(sessions, now)),
    [sessions, now],
  );

  return (
    <nav aria-label="Conversations" className="flex flex-col gap-4">
      <button
        type="button"
        onClick={onNew}
        disabled={busy}
        className="flex items-center justify-center gap-2 rounded-xl border border-parchment/18 px-3 py-2.5
                   text-sm text-parchment transition hover:border-indigo/60 hover:bg-indigo/12
                   disabled:opacity-50"
      >
        <span aria-hidden="true" className="text-base leading-none">
          +
        </span>
        New conversation
      </button>

      {sessions.length === 0 ? (
        <p className="px-1 text-xs leading-relaxed text-parchment-dim">
          Your conversations will collect here.
        </p>
      ) : groups === null ? (
        <ul className="flex flex-col gap-0.5">
          {sessions.map((session) => (
            <li key={session.id}>
              <button
                type="button"
                onClick={() => onSelect(session.id)}
                aria-current={session.id === activeId}
                className="thread truncate text-sm"
                title={session.title ?? "New conversation"}
              >
                {session.title ?? "New conversation"}
              </button>
            </li>
          ))}
        </ul>
      ) : (
        <div className="flex flex-col gap-4">
          {groups.map((group) => (
            <section key={group.label} className="flex flex-col gap-1">
              <h2 className="px-1 pb-1 font-mono text-[10px] font-medium tracking-[0.02em] text-parchment-dim/70">
                {group.label}
              </h2>
              <ul className="flex flex-col gap-0.5">
                {group.sessions.map((session) => (
                  <li key={session.id}>
                    <button
                      type="button"
                      onClick={() => onSelect(session.id)}
                      // aria-current takes the id comparison directly so the styling
                      // and the accessibility tree can never disagree about which
                      // conversation is open.
                      aria-current={session.id === activeId}
                      className="thread truncate text-sm"
                      title={session.title ?? "New conversation"}
                    >
                      {session.title ?? "New conversation"}
                    </button>
                  </li>
                ))}
              </ul>
            </section>
          ))}
        </div>
      )}
    </nav>
  );
}
