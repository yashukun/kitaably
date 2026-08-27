"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

import { GlassCard, Eyebrow, TextLink } from "@/components/glass";
import { createClient } from "@/lib/supabase/client";

/**
 * One sign-in page for everybody, landing everybody in the same place.
 *
 * There is nothing to branch on: one kind of account, one dashboard. The page that
 * used to read `profiles.role` to decide where to send you no longer needs to ask.
 */
export function LoginForm({ next }: { next: string }) {
  const router = useRouter();
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function signIn(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);

    setBusy(true);
    setError(null);

    const { error: signInError } = await createClient().auth.signInWithPassword({
      email: String(data.get("email") ?? ""),
      password: String(data.get("password") ?? ""),
    });

    if (signInError) {
      setError(signInError.message);
      setBusy(false);
      return;
    }

    // refresh() so the server layout re-runs against the cookie that was just set.
    router.push(next);
    router.refresh();
  }

  return (
    <main className="flex flex-1 items-center justify-center px-5 py-16">
      <GlassCard raised className="rise w-full max-w-md p-8 sm:p-10">
        <Eyebrow>Kitaably</Eyebrow>
        <h1 className="mt-3 font-display text-3xl font-semibold tracking-tight">Sign in</h1>
        <p className="mt-2 text-sm leading-relaxed text-parchment-dim">
          Your books, your notes, your papers.
        </p>

        <form onSubmit={signIn} className="mt-8 flex flex-col gap-4">
          <label className="flex flex-col gap-2">
            <span className="font-mono text-[11px] font-medium tracking-[0.02em] text-parchment-dim">
              Email
            </span>
            <input
              name="email"
              type="email"
              required
              autoComplete="email"
              className="field px-4 py-2.5 text-sm"
              placeholder="you@school.edu"
            />
          </label>

          <label className="flex flex-col gap-2">
            <span className="font-mono text-[11px] font-medium tracking-[0.02em] text-parchment-dim">
              Password
            </span>
            <input
              name="password"
              type="password"
              required
              autoComplete="current-password"
              className="field px-4 py-2.5 text-sm"
              placeholder="••••••••••"
            />
          </label>

          {error && (
            <p
              role="alert"
              className="rounded-lg border border-danger/40 bg-danger/10 px-3 py-2 text-sm text-danger"
            >
              {error}
            </p>
          )}

          <button
            type="submit"
            disabled={busy}
            className="mt-2 rounded-xl bg-indigo px-4 py-2.5 text-sm font-medium text-parchment transition hover:bg-indigo/85 disabled:opacity-50"
          >
            {busy ? "Signing in…" : "Sign in"}
          </button>
        </form>

        <p className="mt-6 text-xs text-parchment-dim">
          No account? <TextLink href="/signup">Create one</TextLink>
        </p>
        <p className="mt-2 text-xs text-parchment-dim">
          <TextLink href="/forgot-password">Forgot your password?</TextLink>
        </p>
      </GlassCard>
    </main>
  );
}
