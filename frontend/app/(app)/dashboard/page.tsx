import Link from "next/link";

import { GlassCard, Eyebrow, Shell } from "@/components/glass";
import { createClient } from "@/lib/supabase/server";

export const metadata = { title: "Dashboard" };

const CARDS = [
  {
    href: "/books",
    eyebrow: "Material",
    title: "Books",
    body: "Upload what you are studying from. Private by default; share a book and everyone signed in can read it.",
  },
  {
    href: "/chat",
    eyebrow: "Tutor",
    title: "Ask the books",
    body: "Questions answered from the material, with the page attached — or a plain refusal when it is not covered.",
  },
  {
    href: "/assessments",
    eyebrow: "Phases 5–6",
    title: "Assessments",
    body: "Generate a paper from shared chapters, publish it, and share the link. Not built yet.",
  },
] as const;

export default async function DashboardPage() {
  const supabase = await createClient();
  const {
    data: { user },
  } = await supabase.auth.getUser();

  const { data: profile } = await supabase
    .from("profiles")
    .select("name")
    .eq("id", user!.id)
    .single();

  const greeting = profile?.name?.split(" ")[0] ?? "there";

  return (
    <Shell eyebrow="Signed in" title={`Hello, ${greeting}`}>
      <p className="mb-8 max-w-2xl text-sm leading-relaxed text-parchment-dim">
        One account, everything in it. Upload material, ask it questions, and build a
        paper from what you have shared.
      </p>

      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        {CARDS.map((card) => (
          <Link key={card.href} href={card.href} className="group">
            <GlassCard className="flex h-full flex-col p-6 transition group-hover:border-parchment/25">
              <Eyebrow>{card.eyebrow}</Eyebrow>
              <p className="mt-2.5 font-display text-xl font-semibold">{card.title}</p>
              <p className="mt-2 text-sm leading-relaxed text-parchment-dim">{card.body}</p>
            </GlassCard>
          </Link>
        ))}
      </div>
    </Shell>
  );
}
