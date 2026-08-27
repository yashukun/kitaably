"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { GlassCard, Eyebrow, TextLink } from "@/components/glass";
import { createClient } from "@/lib/supabase/client";
import { MIN_PASSWORD_LENGTH } from "@/lib/password";

/**
 * Set a new password, using the one-shot session the recovery link established.
 *
 * Reached only from `/auth/callback`, which spent the emailed token and put a real
 * session in the cookie. So there is no token handled here and none in this
 * component's props — the credential was consumed server-side, and what remains is
 * an ordinary authenticated `updateUser`.
 *
 * Every other session is signed out afterwards. Somebody resetting a password may
 * be doing it because a session they did not open is still alive somewhere, and
 * leaving those running makes the reset cosmetic.
 */
export function ResetPasswordForm() {
  const router = useRouter();
  const [error, setError] = useState<string | null>(null);
  const [ready, setReady] = useState(false);
  const [busy, setBusy] = useState(false);

  // The middleware already bounced anyone with no session at all. This second look
  // is for the narrower case it cannot see: a session that exists but is not a
  // recovery one, i.e. somebody who navigated here by hand.
  useEffect(() => {
    createClient()
      .auth.getUser()
      .then(({ data }) => {
        if (!data.user) {
          setError("That link has expired. Request a new one.");
        }
        setReady(true);
      });
  }, []);

  async function setPassword(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    const password = String(data.get("password") ?? "");

    if (password !== String(data.get("confirm") ?? "")) {
      setError("Those two passwords do not match.");
      return;
    }

    setBusy(true);
    setError(null);

    const supabase = createClient();
    const { error: updateError } = await supabase.auth.updateUser({ password });

    if (updateError) {
      setError(updateError.message);
      setBusy(false);
      return;
    }

    // `others` keeps the session that just did the reset and revokes the rest, so
    // the person changing the password is not immediately logged out of the tab
    // they are standing in.
    await supabase.auth.signOut({ scope: "others" });

    router.push("/dashboard");
    router.refresh();
  }

  return (
    <main className="flex flex-1 items-center justify-center px-5 py-16">
      <GlassCard raised className="rise w-full max-w-md p-8 sm:p-10">
        <Eyebrow>Kitaably</Eyebrow>
        <h1 className="mt-3 font-display text-3xl font-semibold tracking-tight">
          Choose a new password
        </h1>
        <p className="mt-2 text-sm leading-relaxed text-parchment-dim">
          Signing you in on this device. Everywhere else gets signed out.
        </p>

        <form onSubmit={setPassword} className="mt-8 flex flex-col gap-4">
          <label className="flex flex-col gap-2">
            <span className="font-mono text-[11px] font-medium tracking-[0.02em] text-parchment-dim">
              New password
            </span>
            <input
              name="password"
              type="password"
              required
              minLength={MIN_PASSWORD_LENGTH}
              autoComplete="new-password"
              className="field px-4 py-2.5 text-sm"
              placeholder={`At least ${MIN_PASSWORD_LENGTH} characters`}
            />
          </label>

          <label className="flex flex-col gap-2">
            <span className="font-mono text-[11px] font-medium tracking-[0.02em] text-parchment-dim">
              Confirm
            </span>
            <input
              name="confirm"
              type="password"
              required
              minLength={MIN_PASSWORD_LENGTH}
              autoComplete="new-password"
              className="field px-4 py-2.5 text-sm"
              placeholder="Type it again"
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
            disabled={busy || !ready}
            className="mt-2 rounded-xl bg-indigo px-4 py-2.5 text-sm font-medium text-parchment transition hover:bg-indigo/85 disabled:opacity-50"
          >
            {busy ? "Saving…" : "Save password"}
          </button>
        </form>

        <p className="mt-6 text-xs text-parchment-dim">
          Link expired? <TextLink href="/forgot-password">Request another</TextLink>
        </p>
      </GlassCard>
    </main>
  );
}
