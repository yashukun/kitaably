"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";

import { createClient } from "@/lib/supabase/client";

const LINKS = [
  { href: "/dashboard", label: "Dashboard" },
  { href: "/books", label: "Books" },
  { href: "/chat", label: "Tutor" },
  { href: "/assessments", label: "Assessments" },
] as const;

export function SiteNav({ name }: { name: string }) {
  const pathname = usePathname();
  const router = useRouter();

  async function signOut() {
    await createClient().auth.signOut();
    // refresh() so the server layout re-runs and sees the cleared cookie; without
    // it the client navigates while the cached RSC payload still says signed in.
    router.push("/login");
    router.refresh();
  }

  return (
    <header className="sticky top-0 z-20 border-b border-parchment/10 bg-ink/70 backdrop-blur-xl">
      <div className="mx-auto flex w-full max-w-5xl flex-wrap items-center gap-x-6 gap-y-3 px-5 py-3 sm:px-8">
        <Link href="/dashboard" className="font-display text-lg font-semibold tracking-tight">
          Kitaably
        </Link>

        <nav className="flex flex-1 flex-wrap items-center gap-1">
          {LINKS.map((link) => {
            const active = pathname === link.href || pathname.startsWith(`${link.href}/`);
            return (
              <Link
                key={link.href}
                href={link.href}
                aria-current={active ? "page" : undefined}
                className={`rounded-lg px-3 py-1.5 text-sm transition ${
                  active
                    ? "bg-parchment/12 text-parchment"
                    : "text-parchment-dim hover:text-parchment"
                }`}
              >
                {link.label}
              </Link>
            );
          })}
        </nav>

        <div className="flex items-center gap-3">
          <span className="hidden text-xs text-parchment-dim sm:inline">{name}</span>
          <button
            onClick={signOut}
            className="rounded-lg border border-parchment/18 px-3 py-1.5 text-xs text-parchment-dim transition hover:border-parchment/35 hover:text-parchment"
          >
            Sign out
          </button>
        </div>
      </div>
    </header>
  );
}
