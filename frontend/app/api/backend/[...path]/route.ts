import type { NextRequest } from "next/server";

/**
 * Runtime proxy: /api/backend/* -> FastAPI /api/v1/*
 *
 * This is deliberately a Route Handler rather than a `next.config.ts` rewrite.
 * Rewrites are evaluated at BUILD time and baked into routes-manifest.json, so the
 * destination would freeze to whatever BACKEND_INTERNAL_URL was when the image was
 * built — `http://localhost:8000` in CI, which is the frontend container itself at
 * runtime. Making it a build arg instead would produce an environment-specific
 * image, and ARCHITECTURE.md is explicit that the same image runs everywhere with
 * only the overlay differing. Reading the variable per request is what actually
 * satisfies that.
 *
 * It also gives Phase 4 a streaming path it can trust: the upstream body is handed
 * straight back, so SSE from the chat endpoint is not buffered on its way out.
 *
 * The browser only ever talks to its own origin, so the backend is never directly
 * addressable from a page and no CORS preflight is involved.
 */

export const dynamic = "force-dynamic";

// Hop-by-hop and body-framing headers. Node's fetch has already decompressed the
// upstream body, so forwarding its content-encoding would hand the browser a
// gzip label on plain bytes.
const STRIPPED_RESPONSE_HEADERS = [
  "content-encoding",
  "content-length",
  "transfer-encoding",
  "connection",
];

async function forward(
  request: NextRequest,
  context: { params: Promise<{ path: string[] }> },
): Promise<Response> {
  const backend = process.env.BACKEND_INTERNAL_URL ?? "http://localhost:8000";
  const { path } = await context.params;

  const target = new URL(`${backend}/api/v1/${path.join("/")}`);
  target.search = request.nextUrl.search;

  const headers = new Headers(request.headers);
  // The upstream must not see the browser's Host, and fetch reframes the body.
  headers.delete("host");
  headers.delete("content-length");
  headers.delete("connection");
  // Node's fetch REJECTS this one outright ("expect header not supported"), so
  // forwarding it turns the whole request into a 503 that names the backend for a
  // fault that is entirely ours. Browsers never send it; curl adds it on its own
  // for any large body, which is every book upload from a script or from Bruno.
  headers.delete("expect");

  const hasBody = request.method !== "GET" && request.method !== "HEAD";

  let upstream: Response;
  try {
    upstream = await fetch(target, {
      method: request.method,
      headers,
      body: hasBody ? request.body : undefined,
      // Required by Node whenever a request body is a stream.
      ...(hasBody ? { duplex: "half" } : {}),
      redirect: "manual",
      cache: "no-store",
    } as RequestInit);
  } catch (caught) {
    // Say everything to the operator. This branch answers 503 whatever went wrong,
    // so without a log line a failed upload is a status code and no cause at all —
    // which is exactly how a body-size limit spent an afternoon looking like "the
    // backend is down".
    console.error(
      JSON.stringify({
        level: "ERROR",
        message: "proxy request failed",
        method: request.method,
        path: `/api/v1/${path.join("/")}`,
        error: caught instanceof Error ? `${caught.name}: ${caught.message}` : String(caught),
        cause:
          caught instanceof Error && caught.cause instanceof Error
            ? `${caught.cause.name}: ${caught.cause.message}`
            : undefined,
      }),
    );
    // The backend being down is not a frontend crash. Answer in the backend's own
    // error envelope so one client-side handler covers both cases.
    return Response.json(
      {
        error: {
          code: "upstream_unavailable",
          message: "A service this depends on is unavailable.",
        },
      },
      { status: 503 },
    );
  }

  const responseHeaders = new Headers(upstream.headers);
  for (const header of STRIPPED_RESPONSE_HEADERS) responseHeaders.delete(header);

  return new Response(upstream.body, {
    status: upstream.status,
    statusText: upstream.statusText,
    headers: responseHeaders,
  });
}

export const GET = forward;
export const POST = forward;
export const PUT = forward;
export const PATCH = forward;
export const DELETE = forward;
export const HEAD = forward;
export const OPTIONS = forward;
