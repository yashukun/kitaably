import { createServerClient } from "@supabase/ssr";

import { AUTH_COOKIE_NAME, serverSupabaseConfig } from "@/lib/supabase/config";
import { safeNext } from "@/lib/next-path";
import { NextResponse, type NextRequest } from "next/server";

/**
 * Refresh the Supabase session on every request, and keep signed-out visitors out
 * of the signed-in area.
 *
 * **This redirect is a convenience, not a security boundary.** It stops a signed-out
 * visitor landing on a shell full of controls that would fail anyway. Anyone can
 * skip it entirely by calling the API directly, which is fine — the backend guard
 * is what actually refuses the request, and it re-reads the caller from `profiles`
 * every time.
 *
 * There is no role check here any more, because there is one kind of account. What
 * remains is the only question worth asking at this layer: is anybody signed in.
 */

/** Paths that require a session. Everything else is public.
 *
 * `/exam` is here even though the share link is the access grant: the token grants
 * access to *attempt*, and a result has to belong to somebody to come back to the
 * paper's author. Somebody arriving on a link while signed out is sent to sign in and
 * returned to the exam, rather than being told the link is broken. */
const PROTECTED = [
  "/dashboard",
  "/books",
  "/chat",
  "/assessments",
  "/exam",
  "/attempt",
  // Reachable only with the session `/auth/callback` minted from a recovery link.
  // Listed here so arriving without one lands on sign-in rather than on a form whose
  // submit would fail; the real refusal is GoTrue rejecting an unauthenticated
  // `updateUser`, exactly as with every other route on this list.
  "/reset-password",
];

/** Signed-in users have no business on these.
 *
 * `/forgot-password` is deliberately NOT here, and `/auth/callback` cannot be: both
 * are visited *while holding a session* on the recovery path. The callback mints one
 * and hands off to `/reset-password`, and a failed exchange bounces back to
 * `/forgot-password?error=…` — which, listed here, would redirect to the dashboard
 * and swallow the error. Somebody signed in who wants to change their password is
 * also entitled to ask for a link. */
const SIGNED_OUT_ONLY = ["/login", "/signup"];

const matches = (pathname: string, prefixes: string[]) =>
  prefixes.some((prefix) => pathname === prefix || pathname.startsWith(`${prefix}/`));

export async function updateSession(request: NextRequest) {
  const { url, anonKey } = serverSupabaseConfig();

  // Supabase may not be configured yet — `supabase start` has not been run and the
  // keys in .env are still blank. Creating a client with empty values throws, and
  // because this runs on every matched request it would turn the entire app into a
  // 500. Pass the request through instead: there is no session to refresh, and
  // routes that genuinely need one are refused by the backend guard, which is the
  // real boundary regardless of what happens here.
  if (!url || !anonKey) {
    return NextResponse.next({ request });
  }

  let response = NextResponse.next({ request });

  const supabase = createServerClient(url, anonKey, {
    cookieOptions: { name: AUTH_COOKIE_NAME },
    cookies: {
      getAll() {
        return request.cookies.getAll();
      },
      setAll(cookiesToSet) {
        cookiesToSet.forEach(({ name, value }) => request.cookies.set(name, value));
        response = NextResponse.next({ request });
        cookiesToSet.forEach(({ name, value, options }) =>
          response.cookies.set(name, value, options),
        );
      },
    },
  });

  // Do not remove: this call is what actually refreshes an expiring token.
  const {
    data: { user },
  } = await supabase.auth.getUser();

  const { pathname } = request.nextUrl;

  // A redirect issued here must carry the cookies the refresh above just set, or
  // the very next request arrives with the stale token and loops.
  const redirectTo = (target: string, next?: string) => {
    const location = request.nextUrl.clone();
    location.pathname = target;
    location.search = next ? `?next=${encodeURIComponent(next)}` : "";
    const redirect = NextResponse.redirect(location);
    response.cookies.getAll().forEach((cookie) => redirect.cookies.set(cookie));
    return redirect;
  };

  if (!user && matches(pathname, PROTECTED)) {
    // Carry where they were going. Somebody handed an exam link should land on the
    // exam after signing up, not on a dashboard wondering where the link went.
    return redirectTo("/login", pathname);
  }

  if (user && matches(pathname, SIGNED_OUT_ONLY)) {
    // `next` is only ever written by the branch above, but it arrives here through
    // the URL and is therefore a claim. `safeNext` is the one place that rule lives.
    return redirectTo(safeNext(request.nextUrl.searchParams.get("next")));
  }

  return response;
}
