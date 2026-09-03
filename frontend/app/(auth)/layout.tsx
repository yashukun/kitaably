import Link from "next/link";

/**
 * Signed-out shell. Phase 1.
 *
 * Sign-in is Supabase Auth, email and password. The session lands in httpOnly
 * cookies via @supabase/ssr — never localStorage, never a token read from JS.
 *
 * The header is just the wordmark: a way back to the landing page, with none of
 * its calls to action — you are already standing on them.
 */
export default function AuthLayout({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex flex-1 flex-col">
      <header className="mx-auto w-full max-w-6xl px-5 py-4 sm:px-8">
        <Link href="/" className="font-display text-lg font-semibold tracking-tight">
          Kitaably
        </Link>
      </header>
      {children}
    </div>
  );
}
