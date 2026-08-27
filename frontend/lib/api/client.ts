/**
 * The one typed client for the FastAPI backend.
 *
 * Requests go to /api/backend/*, which a Route Handler proxies to the backend's
 * /api/v1/* at request time — so the browser stays same-origin and the backend is
 * never addressable from a page.
 *
 * The access token is read from the Supabase session and sent as a bearer. It is
 * never stored anywhere by this file.
 */

import { createClient } from "@/lib/supabase/client";

export type ApiError = {
  error: { code: string; message: string; request_id?: string };
};

export class ApiRequestError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
    readonly requestId?: string,
  ) {
    super(message);
    this.name = "ApiRequestError";
  }
}

/** Cursor pagination, mirroring the backend's `Page` schema. */
export type Page<T> = {
  items: T[];
  next_cursor: string | null;
};

const BASE = "/api/backend";

async function authHeader(): Promise<Record<string, string>> {
  const {
    data: { session },
  } = await createClient().auth.getSession();
  return session ? { Authorization: `Bearer ${session.access_token}` } : {};
}

async function raise(response: Response): Promise<never> {
  // The backend's error envelope is the contract; anything else is a proxy or a
  // crash, and gets a generic message rather than a parse failure.
  const body = (await response.json().catch(() => null)) as ApiError | null;
  throw new ApiRequestError(
    response.status,
    body?.error.code ?? "unknown",
    body?.error.message ?? "Something went wrong.",
    body?.error.request_id,
  );
}

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${BASE}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(await authHeader()),
      ...init?.headers,
    },
  });

  if (!response.ok) return raise(response);
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

/**
 * A file download. Returns the body as a Blob plus the server's suggested
 * filename from Content-Disposition — null when the proxy stripped the header,
 * so the caller supplies its own fallback rather than saving "undefined".
 */
export async function apiDownload(
  path: string,
): Promise<{ blob: Blob; filename: string | null }> {
  const response = await fetch(`${BASE}${path}`, {
    headers: { ...(await authHeader()) },
  });
  if (!response.ok) return raise(response);

  const disposition = response.headers.get("content-disposition") ?? "";
  const match = /filename="([^"]+)"/.exec(disposition);
  return { blob: await response.blob(), filename: match?.[1] ?? null };
}

/**
 * Multipart upload. Content-Type is left to the browser so it sets the boundary.
 *
 * XMLHttpRequest rather than fetch, for one reason: `fetch` reports nothing about
 * how far a request BODY has got. A book is up to 80 MB, so "Uploading…" with no
 * number attached is several silent minutes in which the only honest reading is that
 * the page has hung. `xhr.upload.onprogress` is still the only way to know.
 */
export async function apiUpload<T>(
  path: string,
  form: FormData,
  onProgress?: (fraction: number) => void,
): Promise<T> {
  const headers = await authHeader();

  return new Promise<T>((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `${BASE}${path}`);
    for (const [key, value] of Object.entries(headers)) xhr.setRequestHeader(key, value);

    if (onProgress) {
      xhr.upload.onprogress = (event) => {
        // `lengthComputable` is false for a chunked body; report nothing rather than
        // inventing a percentage, and let the caller fall back to an indeterminate bar.
        if (event.lengthComputable && event.total > 0) onProgress(event.loaded / event.total);
      };
    }

    const fail = (status: number, code: string, text: string) =>
      reject(new ApiRequestError(status, code, text));

    xhr.onload = () => {
      const body = (() => {
        try {
          return JSON.parse(xhr.responseText) as T & Partial<ApiError>;
        } catch {
          return null;
        }
      })();

      if (xhr.status >= 200 && xhr.status < 300) {
        if (body === null) return fail(xhr.status, "bad_response", "Malformed response.");
        return resolve(body as T);
      }
      // The backend's error envelope is the contract; anything else is a proxy or a
      // crash, and gets a generic message rather than a parse failure.
      const envelope = (body as Partial<ApiError> | null)?.error;
      fail(xhr.status, envelope?.code ?? "unknown", envelope?.message ?? "Upload failed.");
    };

    xhr.onerror = () => fail(0, "network_error", "The upload could not reach the server.");
    xhr.onabort = () => fail(0, "aborted", "Upload cancelled.");
    xhr.ontimeout = () => fail(0, "timeout", "The upload timed out.");

    xhr.send(form);
  });
}

/** Open an SSE stream over POST. EventSource cannot do this: it is GET-only and
 *  cannot carry an Authorization header. */
export async function apiStream(
  path: string,
  body: unknown,
  onEvent: (event: string, data: Record<string, unknown>) => void,
  signal?: AbortSignal,
): Promise<void> {
  const response = await fetch(`${BASE}${path}`, {
    method: "POST",
    body: JSON.stringify(body),
    headers: { "Content-Type": "application/json", ...(await authHeader()) },
    signal,
  });

  if (!response.ok) return raise(response);
  if (!response.body) throw new ApiRequestError(500, "no_body", "No response body.");

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    // SSE frames are separated by a blank line. Anything after the last blank line
    // is a partial frame and stays in the buffer.
    const frames = buffer.split("\n\n");
    buffer = frames.pop() ?? "";

    for (const frame of frames) {
      let name = "message";
      let payload = "";
      for (const line of frame.split("\n")) {
        if (line.startsWith("event: ")) name = line.slice(7).trim();
        else if (line.startsWith("data: ")) payload += line.slice(6);
      }
      if (payload) onEvent(name, JSON.parse(payload));
    }
  }
}
