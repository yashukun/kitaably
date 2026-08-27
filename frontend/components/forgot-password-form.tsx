"use client";

import { useState } from "react";

import { GlassCard, Eyebrow, TextLink } from "@/components/glass";
import { createClient } from "@/lib/supabase/client";

/**
 * Request a recovery link.
 *
 * **The response never says whether the address has an account.** Success and "no
 * such user" produce the same sentence, because this form is unauthenticated and a
 * distinguishable answer turns it into a membership oracle — feed it a list of
 * addresses and it tells you which ones are registered here. Supabase's own
 * `resetPasswordForEmail` returns success either way for exactly this reason; the
 * copy has to match, or the leak comes back at the UI layer.
 */
export function ForgotPasswordForm({ initialError }: { initialError?: string }) {
  const [error, setError] = useState<string | null>(initialError ?? null);
  const [sent, setSent] = useState(false);
  const [busy, setBusy] = useState(false);

  async function requestLink(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);

    setBusy(true);
    setError(null);

    const { error: sendError } = await createClient().auth.resetPasswordForEmail(
      String(data.get("email") ?? ""),
      // Absolute, and built from the live origin rather than a baked-in env var:
      // NEXT_PUBLIC_* is fixed at build time, so one image would otherwise mail
      // localhost links from production. GoTrue still refuses any origin missing
      // from `additional_redirect_urls`, so this is a request, not a bypass.
      { redirectTo: `${window.location.origin}/auth/callback?next=/reset-password` },
    );

    if (sendError) {
      // Rate limiting is the realistic failure here, and it is worth showing —
      // it says "too many", not "that address exists".
      setError(sendError.message);
      setBusy(false);
      return;
    }

    setSent(true);
    setBusy(false);
  }

  return (
    <main className="flex flex-1 items-center justify-center px-5 py-16">
      <GlassCard raised className="rise w-full max-w-md p-8 sm:p-10">
        <Eyebrow>Kitaably</Eyebrow>
        <h1 className="mt-3 font-display text-3xl font-semibold tracking-tight">
          Reset your password
        </h1>
        <p className="mt-2 text-sm leading-relaxed text-parchment-dim">
          We&apos;ll email you a link that signs you in once, so you can set a new one.
        </p>

        {sent ? (
          <p
            role="status"
            className="mt-8 rounded-lg border border-saffron/40 bg-saffron/10 px-3 py-2 text-sm leading-relaxed text-saffron"
          >
            If that address has an account, a reset link is on its way. It expires in
            an hour and can only be used once.
          </p>
        ) : (
          <form onSubmit={requestLink} className="mt-8 flex flex-col gap-4">
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
              {busy ? "Sending…" : "Send reset link"}
            </button>
          </form>
        )}

        <p className="mt-6 text-xs text-parchment-dim">
          Remembered it? <TextLink href="/login">Sign in</TextLink>
        </p>
      </GlassCard>
    </main>
  );
}
