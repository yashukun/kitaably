/**
 * Supabase connection details, and the one cookie name both sides agree on.
 */

/**
 * @supabase/ssr derives its cookie name from the project ref it parses out of the
 * Supabase URL. Locally the browser and the server use DIFFERENT urls — the browser
 * reaches 127.0.0.1, the container reaches host.docker.internal — so each derives a
 * different name and looks for a cookie the other never wrote. The symptom is a
 * session that works perfectly in the browser and does not exist on the server,
 * which reads as "signed out" on every protected route.
 *
 * Pinning the name decouples what the cookie is called from where Supabase lives.
 */
export const AUTH_COOKIE_NAME = "sb-kitaably-auth";

/**
 * Supabase connection details for code running on the SERVER.
 *
 * A browser resolves 127.0.0.1 to the developer's machine; the same string inside a
 * container resolves to the container itself, where nothing is listening. Server
 * code prefers SUPABASE_URL (host.docker.internal in compose, the project URL in
 * deploy) and falls back to the public one when running outside a container.
 */
export function serverSupabaseConfig() {
  return {
    url: process.env.SUPABASE_URL ?? process.env.NEXT_PUBLIC_SUPABASE_URL,
    anonKey: process.env.SUPABASE_ANON_KEY ?? process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY,
  };
}
