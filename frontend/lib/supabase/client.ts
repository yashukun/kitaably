import { createBrowserClient } from "@supabase/ssr";

import { AUTH_COOKIE_NAME } from "@/lib/supabase/config";

/**
 * Supabase client for Client Components.
 *
 * Only NEXT_PUBLIC_* values may appear here — anything in this file is compiled
 * into the browser bundle. The service-role key bypasses RLS and must never come
 * near it.
 */
export function createClient() {
  return createBrowserClient(
    process.env.NEXT_PUBLIC_SUPABASE_URL!,
    process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY!,
    // Must match the server's, or the session it writes is invisible there.
    { cookieOptions: { name: AUTH_COOKIE_NAME } },
  );
}
