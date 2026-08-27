import Link from "next/link";

import { GlassCard, Eyebrow } from "@/components/glass";
import { ScopeChip } from "@/components/scope-chip";

/**
 * The hero is the product's actual thesis rendered as the thing itself: an answer
 * with its sources showing. Not a claim about grounding — the grounding, on screen.
 */
export default function Home() {
  return (
    <main className="mx-auto flex w-full max-w-5xl flex-1 flex-col justify-center gap-14 px-5 py-16 sm:px-8">
      <header className="rise flex max-w-2xl flex-col gap-5">
        <Eyebrow>Kitaably</Eyebrow>
        <h1 className="font-display text-5xl leading-[1.05] font-semibold tracking-tight sm:text-6xl">
          Ask the books you are
          <span className="text-saffron"> actually studying from.</span>
        </h1>
        <p className="max-w-xl text-lg leading-relaxed text-parchment-dim">
          Upload the books you are studying from. Kitaably answers from those pages
          and shows you which one — so you can go and check.
        </p>
      </header>

      <GlassCard raised className="rise p-6 sm:p-8" >
        <Eyebrow>What an answer looks like</Eyebrow>
        <p className="mt-4 text-[15px] leading-relaxed">
          Photosynthesis converts light energy into chemical energy stored as glucose. It
          happens in the chloroplasts, which contain chlorophyll.{" "}
          <span className="font-mono text-xs text-saffron align-super">[1]</span>
        </p>

        <div className="mt-6 grid gap-3 sm:grid-cols-2">
          <div className="slip border-l-canon p-3.5">
            <div className="flex items-center justify-between gap-2">
              <span className="font-mono text-[11px] text-parchment-dim">[1] page 1</span>
              <ScopeChip scope="canon" />
            </div>
            <p className="mt-2 text-sm text-parchment/85">Biology Textbook</p>
          </div>
          <div className="slip border-l-personal p-3.5">
            <div className="flex items-center justify-between gap-2">
              <span className="font-mono text-[11px] text-parchment-dim">[2] page 1</span>
              <ScopeChip scope="personal" />
            </div>
            <p className="mt-2 text-sm text-parchment/85">Your revision notes</p>
          </div>
        </div>

        <p className="mt-5 text-xs leading-relaxed text-parchment-dim">
          Your own uploads stay yours. Nobody else can read them, and they never
          appear in a paper anyone else sits.
        </p>
      </GlassCard>

      <nav className="rise flex flex-wrap gap-3">
        <Link
          href="/signup"
          className="rounded-xl bg-indigo px-5 py-2.5 text-sm font-medium transition hover:bg-indigo/85"
        >
          Create an account
        </Link>
        <Link
          href="/login"
          className="glass rounded-xl px-5 py-2.5 text-sm transition hover:border-parchment/25"
        >
          Sign in
        </Link>
      </nav>

    </main>
  );
}
