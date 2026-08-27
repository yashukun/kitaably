"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

import { GlassCard, Eyebrow, TextLink } from "@/components/glass";
import { createClient } from "@/lib/supabase/client";
import { MIN_PASSWORD_LENGTH } from "@/lib/password";

/**
 * Create an account. There is no role to pick — there is one kind of account, and
 * everything the product does is available to it.
 *
 * `name` goes into user metadata, which the signup trigger copies into `profiles`.
 * That is the only thing the trigger reads out of metadata, and it is deliberately
 * a display string: metadata is client-supplied, so nothing that decides what
 * somebody may do is allowed to come from it.
 */
export function SignupForm({ next }: { next: string }) {
  const router = useRouter();
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function signUp(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);

    setBusy(true);
    setError(null);

    const supabase = createClient();
    const { data: created, error: signUpError } = await supabase.auth.signUp({
      email: String(data.get("email") ?? ""),
      password: String(data.get("password") ?? ""),
      options: { data: { name: String(data.get("name") ?? "").trim() } },
    });

    if (signUpError) {
      setError(signUpError.message);
      setBusy(false);
      return;
    }

    // With email confirmation switched on, signUp returns a user and no session.
    // Saying so is better than a redirect to a page that bounces straight back.
    if (!created.session) {
      setNotice("Check your email to confirm the address, then sign in.");
      setBusy(false);
      return;
    }

    router.push(next);
    router.refresh();
  }

  return (
    <main className="flex flex-1 items-center justify-center px-5 py-16">
      <GlassCard raised className="rise w-full max-w-md p-8 sm:p-10">
        <Eyebrow>Kitaably</Eyebrow>
        <h1 className="mt-3 font-display text-3xl font-semibold tracking-tight">
          Create an account
        </h1>
        <p className="mt-2 text-sm leading-relaxed text-parchment-dim">
          Upload your books, ask them questions, build a paper from them.
        </p>

        <form onSubmit={signUp} className="mt-8 flex flex-col gap-4">
          <label className="flex flex-col gap-2">
            <span className="font-mono text-[11px] font-medium tracking-[0.02em] text-parchment-dim">
              Name
            </span>
            <input
              name="name"
              required
              autoComplete="name"
              className="field px-4 py-2.5 text-sm"
              placeholder="Amina Rahman"
            />
          </label>

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
              // Both from the shared constant, which has to agree with
              // `minimum_password_length` in supabase/config.toml. This input is a
              // courtesy -- GoTrue is what actually refuses a short password -- but
              // a courtesy that quotes the wrong number is worse than none.
              minLength={MIN_PASSWORD_LENGTH}
              autoComplete="new-password"
              className="field px-4 py-2.5 text-sm"
              placeholder={`At least ${MIN_PASSWORD_LENGTH} characters`}
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

          {notice && (
            <p
              role="status"
              className="rounded-lg border border-saffron/40 bg-saffron/10 px-3 py-2 text-sm text-saffron"
            >
              {notice}
            </p>
          )}

          <button
            type="submit"
            disabled={busy}
            className="mt-2 rounded-xl bg-indigo px-4 py-2.5 text-sm font-medium text-parchment transition hover:bg-indigo/85 disabled:opacity-50"
          >
            {busy ? "Creating…" : "Create account"}
          </button>
        </form>

        <p className="mt-6 text-xs text-parchment-dim">
          Already have one? <TextLink href="/login">Sign in</TextLink>
        </p>
      </GlassCard>
    </main>
  );
}
