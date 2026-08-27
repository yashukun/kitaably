import { createServerClient } from "@supabase/ssr";

import { AUTH_COOKIE_NAME, serverSupabaseConfig } from "@/lib/supabase/config";
import { cookies } from "next/headers";

/**
 * Supabase client for Server Components, Route Handlers, and Server Actions.
 *
 * The session lives in httpOnly cookies. No token is ever read from JS, and none
 * is ever put in localStorage.
 */
export async function createClient() {
  const cookieStore = await cookies();

  const { url, anonKey } = serverSupabaseConfig();

  return createServerClient(url!, anonKey!, {
      cookieOptions: { name: AUTH_COOKIE_NAME },
      cookies: {
        getAll() {
          return cookieStore.getAll();
        },
        setAll(cookiesToSet) {
          try {
            cookiesToSet.forEach(({ name, value, options }) =>
              cookieStore.set(name, value, options),
            );
          } catch {
            // Called from a Server Component, which cannot set cookies. The
            // middleware refresh path handles it instead.
          }
        },
      },
    },
  );
}
