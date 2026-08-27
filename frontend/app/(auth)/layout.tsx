/**
 * Signed-out shell. Phase 1.
 *
 * Sign-in is Supabase Auth, email and password. The session lands in httpOnly
 * cookies via @supabase/ssr — never localStorage, never a token read from JS.
 */
export default function AuthLayout({ children }: { children: React.ReactNode }) {
  return <div className="flex flex-1 flex-col">{children}</div>;
}
