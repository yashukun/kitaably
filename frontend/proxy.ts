import type { NextRequest } from "next/server";

import { updateSession } from "@/lib/supabase/middleware";

/**
 * Runs before every matched request. Next 16 calls this convention `proxy`
 * (it replaced `middleware`).
 *
 * Its only job right now is refreshing the Supabase session cookie. Phase 1 adds
 * the role-based redirect on top — which is a convenience for the user and not a
 * security boundary. The backend guard is what refuses a request.
 */
export default async function proxy(request: NextRequest) {
  return await updateSession(request);
}

export const config = {
  matcher: [
    // Everything except static assets and image files.
    "/((?!_next/static|_next/image|favicon.ico|.*\\.(?:svg|png|jpg|jpeg|gif|webp)$).*)",
  ],
};
