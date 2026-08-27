import { NextResponse, type NextRequest } from "next/server";
import type { EmailOtpType } from "@supabase/supabase-js";

import { createClient } from "@/lib/supabase/server";
import { safeNext } from "@/lib/next-path";

/**
 * Where every emailed auth link lands: password recovery today, address
 * confirmation the moment `enable_confirmations` is switched on in deploy.
 *
 * `supabase/config.toml` has listed this path under `additional_redirect_urls`
 * since Phase 1. It did not exist, so every one of those links would have resolved
 * to a 404 — which is the whole reason there was no way to reset a password.
 *
 * Public by design: the caller is by definition signed out, and the single-use
 * token in the URL is the credential. It is spent here, server-side, and traded for
 * an httpOnly cookie — the token never reaches client JS.
 *
 * Two shapes arrive here, because GoTrue's email templates differ by flow:
 *
 *   ?code=...              PKCE. What @supabase/ssr's browser client asks for, so
 *                          it is what this app gets today.
 *   ?token_hash=&type=     the older template. Handled too, so that swapping a
 *                          template in the dashboard cannot quietly break recovery
 *                          in production.
 */
export async function GET(request: NextRequest) {
  const { searchParams } = request.nextUrl;

  const code = searchParams.get("code");
  const tokenHash = searchParams.get("token_hash");
  const type = searchParams.get("type") as EmailOtpType | null;
  const next = safeNext(searchParams.get("next"));

  // GoTrue reports a refused link by redirecting here with its own error params
  // rather than by failing the exchange below.
  const providerError =
    searchParams.get("error_description") ?? searchParams.get("error");
  if (providerError) {
    return failed(providerError);
  }

  if (!code && !tokenHash) {
    return failed("That link is missing its token.");
  }

  const supabase = await createClient();

  // Both calls set the session cookie through the server client's setAll.
  const { error } = code
    ? await supabase.auth.exchangeCodeForSession(code)
    : await supabase.auth.verifyOtp({ token_hash: tokenHash!, type: type ?? "email" });

  if (error) {
    // Deliberately not echoed to the user. GoTrue distinguishes "already used" from
    // "expired" from "wrong browser", and telling an unauthenticated caller which
    // one it was describes a link they may not own. The log keeps the detail.
    console.error("auth callback exchange failed", {
      message: error.message,
      status: error.status,
    });
    return failed("That link has expired or was already used.");
  }

  // A recovery link means "prove you can read this inbox", not "you are done". The
  // session it just minted can change the password, so send them to do exactly that
  // rather than dropping them on a dashboard with a password they still do not know.
  const destination = type && type !== "recovery" ? "/dashboard" : next;
  return redirect(destination);
}

function failed(reason: string) {
  return redirect(`/forgot-password?error=${encodeURIComponent(reason)}`);
}

/**
 * A **relative** redirect, deliberately.
 *
 * `NextResponse.redirect` demands an absolute URL, and the obvious way to build one
 * here — `request.nextUrl.origin` — is wrong in exactly this file. Inside a Route
 * Handler on the standalone server, `origin` is the address the process *bound to*,
 * so it comes back `http://0.0.0.0:3000` no matter what `Host` the caller sent. That
 * is not a browser can follow, and behind the ingress this deploys to it would be
 * the cluster-internal name instead of the public one. (The middleware does not have
 * this problem, which is why its redirects are built the other way.)
 *
 * A relative `Location` is valid HTTP — RFC 7231 §7.1.2 — and every browser resolves
 * it against the URL actually requested, which is the origin we wanted all along.
 *
 * Cookies still work: in a Route Handler the `cookies()` store the Supabase server
 * client writes through is attached to whatever response this returns, so the session
 * minted above rides out on this redirect.
 */
function redirect(location: string) {
  return new NextResponse(null, { status: 303, headers: { Location: location } });
}
