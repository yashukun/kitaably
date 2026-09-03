import Link from "next/link";

import { GlassCard, Eyebrow } from "@/components/glass";
import { Reveal } from "@/components/reveal";
import { ScopeChip } from "@/components/scope-chip";
import { serverSupabaseConfig } from "@/lib/supabase/config";
import { createClient } from "@/lib/supabase/server";

/**
 * The landing page: the product loop, told in order, with each step shown as the
 * thing itself — an answer with its sources, a stage bar, an observation log —
 * rather than claims about them.
 */

/** Whether anybody is signed in. Tolerates an unconfigured Supabase (blank .env
 *  before `supabase start`), where the answer is simply "no". */
async function isSignedIn(): Promise<boolean> {
  const { url, anonKey } = serverSupabaseConfig();
  if (!url || !anonKey) return false;
  const supabase = await createClient();
  const {
    data: { user },
  } = await supabase.auth.getUser();
  return Boolean(user);
}

function MockChip({ children }: { children: React.ReactNode }) {
  return (
    <span className="rounded-full border border-parchment/18 px-2.5 py-0.5 font-mono text-[11px] text-parchment-dim">
      {children}
    </span>
  );
}

const STEPS: {
  step: string;
  title: string;
  body: string;
  visual: React.ReactNode;
}[] = [
  {
    step: "Step 1",
    title: "Upload what you study from",
    body: "PDF, DOCX, PPTX, plain text or Markdown. Every upload starts private: nobody else can read it, and it never feeds a paper anyone else writes. Share a book when you choose, and it joins the library every signed-in reader can draw from.",
    visual: (
      <>
        <div className="flex flex-wrap items-center justify-between gap-2">
          <p className="font-display text-lg font-semibold">Cell Biology, 3rd ed.</p>
          <ScopeChip scope="personal" />
        </div>
        <p className="mt-1 font-mono text-[11px] text-parchment-dim">pdf · 412 pages</p>
        <div className="mt-4 flex items-center gap-1.5" aria-hidden>
          <span className="h-1 flex-1 rounded-full bg-saffron/80" />
          <span className="h-1 flex-1 rounded-full bg-saffron/80" />
          <span className="h-1 flex-1 rounded-full stage-active" />
          <span className="h-1 flex-1 rounded-full bg-parchment/12" />
          <span className="h-1 flex-1 rounded-full bg-parchment/12" />
        </div>
        <p className="mt-2 text-xs text-parchment-dim">
          <span className="font-mono text-[11px]">Step 3 of 5</span> — cutting it into
          passages a citation can point at.
        </p>
      </>
    ),
  },
  {
    step: "Step 2",
    title: "Ask, and get the page — not a guess",
    body: "The tutor answers only from your material, and every claim carries the passage it came from, so you can open the book and check. When the material does not cover something, it says so instead of inventing an answer.",
    visual: (
      <>
        <p className="text-sm leading-relaxed">
          Mitochondria convert glucose into ATP through cellular respiration.{" "}
          <span className="align-super font-mono text-xs text-saffron">[1]</span>
        </p>
        <div className="slip mt-4 border-l-canon p-3.5">
          <div className="flex items-center justify-between gap-2">
            <span className="font-mono text-[11px] text-parchment-dim">[1] page 87</span>
            <ScopeChip scope="canon" />
          </div>
          <p className="mt-2 text-sm text-parchment/85">Cell Biology, 3rd ed.</p>
        </div>
        <p className="mt-4 border-t border-parchment/10 pt-3 text-xs leading-relaxed text-parchment-dim">
          “Why is the sky blue?” — <span className="text-parchment/75">Your books
          don&rsquo;t cover this.</span> No answer is invented to fill the gap.
        </p>
      </>
    ),
  },
  {
    step: "Step 3",
    title: "Draw a paper from your own shelf",
    body: "Multiple-choice questions, generated across six levels of thinking — from recalling a fact to judging an argument. A paper draws on the shared library and your own uploads, never anyone else's private book. Publish it and send the link.",
    visual: (
      <>
        <div className="flex flex-wrap items-center gap-2">
          <MockChip>Q3 of 10</MockChip>
          <MockChip>understand</MockChip>
          <MockChip>1 mark</MockChip>
        </div>
        <p className="mt-3 text-sm leading-relaxed">
          Why does the inner mitochondrial membrane fold into cristae?
        </p>
        <ul className="mt-3 flex flex-col gap-1.5 text-sm text-parchment-dim">
          <li className="rounded-lg border border-parchment/12 px-3 py-1.5">
            To store genetic material
          </li>
          <li className="rounded-lg border border-indigo/60 bg-indigo/15 px-3 py-1.5 text-parchment">
            To increase surface area for ATP synthesis
          </li>
          <li className="rounded-lg border border-parchment/12 px-3 py-1.5">
            To anchor the cell wall
          </li>
        </ul>
      </>
    ),
  },
  {
    step: "Step 4",
    title: "Sit it with the camera watching, fairly",
    body: "Proctoring runs in the sitter's browser and records observations — never verdicts. What it saw goes to the paper's author alone, and nothing about it is shown to the sitter until the author has read it.",
    visual: (
      <>
        <Eyebrow>Observations</Eyebrow>
        <ul className="mt-3 flex flex-col gap-2 font-mono text-xs text-parchment/80">
          <li className="flex justify-between gap-3 rounded-lg bg-ink/40 px-3 py-2">
            <span>no face detected</span>
            <span className="text-parchment-dim">42s</span>
          </li>
          <li className="flex justify-between gap-3 rounded-lg bg-ink/40 px-3 py-2">
            <span>tab lost focus</span>
            <span className="text-parchment-dim">6s</span>
          </li>
          <li className="flex justify-between gap-3 rounded-lg bg-ink/40 px-3 py-2">
            <span>second face in frame</span>
            <span className="text-parchment-dim">3s</span>
          </li>
        </ul>
        <p className="mt-3 text-xs leading-relaxed text-parchment-dim">
          What they mean is the author&rsquo;s call to make — not the software&rsquo;s.
        </p>
      </>
    ),
  },
  {
    step: "Step 5",
    title: "Review, then release",
    body: "Marks and the proctoring report reach the author first. They read what was observed, decide what it means, and release the result deliberately. No automatic accusation ever reaches the person who sat the paper.",
    visual: (
      <>
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <p className="font-display text-lg font-semibold">Amina Rahman</p>
            <p className="mt-0.5 font-mono text-[11px] text-parchment-dim">
              8/10 · submitted 14:02
            </p>
          </div>
          <span
            aria-hidden
            className="rounded-xl bg-indigo px-4 py-2 text-sm font-medium"
          >
            Release result
          </span>
        </div>
        <p className="mt-4 border-t border-parchment/10 pt-3 text-xs leading-relaxed text-parchment-dim">
          Reviewed: 2 observations read, none worth raising. The sitter sees marks
          and feedback the moment you release — and nothing before.
        </p>
      </>
    ),
  },
];

export default async function Home() {
  const signedIn = await isSignedIn();

  return (
    <>
      <header className="sticky top-0 z-20 border-b border-parchment/10 bg-ink/70 backdrop-blur-xl">
        <div className="mx-auto flex w-full max-w-6xl items-center justify-between gap-4 px-5 py-3 sm:px-8">
          <Link href="/" className="font-display text-lg font-semibold tracking-tight">
            Kitaably
          </Link>
          <nav className="flex items-center gap-2">
            {signedIn ? (
              <Link
                href="/dashboard"
                className="rounded-xl bg-indigo px-4 py-2 text-sm font-medium transition hover:bg-indigo/85"
              >
                Open dashboard
              </Link>
            ) : (
              <>
                <Link
                  href="/login"
                  className="rounded-xl px-4 py-2 text-sm text-parchment-dim transition hover:text-parchment"
                >
                  Sign in
                </Link>
                <Link
                  href="/signup"
                  className="rounded-xl bg-indigo px-4 py-2 text-sm font-medium transition hover:bg-indigo/85"
                >
                  Create an account
                </Link>
              </>
            )}
          </nav>
        </div>
      </header>

      <main className="mx-auto w-full max-w-6xl flex-1 px-5 sm:px-8">
        <section className="grid items-center gap-12 py-20 sm:py-28 lg:grid-cols-[1.1fr_1fr]">
          <div className="rise flex max-w-2xl flex-col gap-5">
            <Eyebrow>Kitaably</Eyebrow>
            <h1 className="text-balance font-display text-5xl leading-[1.05] font-semibold tracking-tight sm:text-6xl">
              Ask the books you are
              <span className="text-saffron"> actually studying from.</span>
            </h1>
            <p className="max-w-xl text-lg leading-relaxed text-parchment-dim">
              Upload your books and they become a tutor that cites its pages — and,
              once shared, the source of proctored papers whose results you review
              before anyone sees them.
            </p>
            <div className="mt-2 flex flex-wrap items-center gap-4">
              <Link
                href={signedIn ? "/dashboard" : "/signup"}
                className="rounded-xl bg-indigo px-5 py-2.5 text-sm font-medium transition hover:bg-indigo/85"
              >
                {signedIn ? "Open dashboard" : "Create an account"}
              </Link>
              <a
                href="#loop"
                className="text-sm text-parchment-dim underline decoration-parchment-dim/30 underline-offset-4 transition hover:text-parchment"
              >
                See how it works
              </a>
            </div>
          </div>

          {/* rise and float are both `animation`, so they live on different
              elements: the wrapper enters, the card levitates. */}
          <div className="rise">
            <GlassCard raised className="float p-6 sm:p-8">
              <Eyebrow>What an answer looks like</Eyebrow>
              <p className="mt-4 text-[15px] leading-relaxed">
                Photosynthesis converts light energy into chemical energy stored as
                glucose. It happens in the chloroplasts, which contain chlorophyll.{" "}
                <span className="align-super font-mono text-xs text-saffron">[1]</span>
              </p>
              <div className="mt-6 grid gap-3 sm:grid-cols-2">
                <div className="slip border-l-canon p-3.5">
                  <div className="flex items-center justify-between gap-2">
                    <span className="font-mono text-[11px] text-parchment-dim">[1] page 12</span>
                    <ScopeChip scope="canon" />
                  </div>
                  <p className="mt-2 text-sm text-parchment/85">Biology Textbook</p>
                </div>
                <div className="slip border-l-personal p-3.5">
                  <div className="flex items-center justify-between gap-2">
                    <span className="font-mono text-[11px] text-parchment-dim">[2] page 4</span>
                    <ScopeChip scope="personal" />
                  </div>
                  <p className="mt-2 text-sm text-parchment/85">Your revision notes</p>
                </div>
              </div>
              <p className="mt-5 text-xs leading-relaxed text-parchment-dim">
                Your own uploads stay yours. Nobody else can read them, and they
                never appear in a paper anyone else sits.
              </p>
            </GlassCard>
          </div>
        </section>

        <section id="loop" className="scroll-mt-24 pb-24">
          <Reveal className="max-w-2xl">
            <Eyebrow>The loop</Eyebrow>
            <h2 className="mt-3 text-balance font-display text-3xl font-semibold tracking-tight sm:text-4xl">
              From your book to a released result.
            </h2>
          </Reveal>

          <div className="mt-14 flex flex-col gap-20">
            {STEPS.map((item, index) => (
              <div
                key={item.step}
                className="grid items-center gap-8 lg:grid-cols-2 lg:gap-14"
              >
                <Reveal className={index % 2 ? "lg:order-2" : ""}>
                  <Eyebrow>{item.step}</Eyebrow>
                  <h3 className="mt-3 text-balance font-display text-2xl font-semibold tracking-tight">
                    {item.title}
                  </h3>
                  <p className="mt-3 max-w-lg text-sm leading-relaxed text-parchment-dim">
                    {item.body}
                  </p>
                </Reveal>
                <Reveal delay={120} className={index % 2 ? "lg:order-1" : ""}>
                  <GlassCard className="lift p-5 sm:p-6">{item.visual}</GlassCard>
                </Reveal>
              </div>
            ))}
          </div>
        </section>

        <section className="pb-24">
          <Reveal>
            <GlassCard raised className="p-8 text-center sm:p-12">
              <h2 className="text-balance font-display text-3xl font-semibold tracking-tight sm:text-4xl">
                One account. Your books, made answerable.
              </h2>
              <p className="mx-auto mt-3 max-w-xl text-sm leading-relaxed text-parchment-dim">
                Everyone can upload, share, write a paper and sit one — there are no
                roles to be assigned. Upload a book and it is answerable in about a
                minute.
              </p>
              <div className="mt-7 flex flex-wrap justify-center gap-3">
                <Link
                  href={signedIn ? "/dashboard" : "/signup"}
                  className="rounded-xl bg-indigo px-5 py-2.5 text-sm font-medium transition hover:bg-indigo/85"
                >
                  {signedIn ? "Open dashboard" : "Create an account"}
                </Link>
                {!signedIn && (
                  <Link
                    href="/login"
                    className="glass rounded-xl px-5 py-2.5 text-sm transition hover:border-parchment/25"
                  >
                    Sign in
                  </Link>
                )}
              </div>
            </GlassCard>
          </Reveal>
        </section>
      </main>

      <footer className="border-t border-parchment/10">
        <div className="mx-auto flex w-full max-w-6xl flex-wrap items-center justify-between gap-2 px-5 py-6 text-xs text-parchment-dim sm:px-8">
          <span className="font-display text-sm text-parchment/80">Kitaably</span>
          <span>Grounded in your books, or silent.</span>
        </div>
      </footer>
    </>
  );
}
