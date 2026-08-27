/**
 * Validate a `?next=` return path.
 *
 * It arrives in a URL, so it is a claim: anybody can write one, and a `next` that is
 * not a same-site absolute path is an open redirect. `//evil.example` and
 * `https://evil.example` are both rejected — the first because browsers read a
 * protocol-relative URL as a different origin, which is exactly the case a naive
 * `startsWith("/")` check waves through.
 *
 * Shared by the middleware and both auth pages so there is one rule rather than
 * three copies of it to drift.
 */
export function safeNext(value: string | null | undefined): string {
  if (!value) return "/dashboard";
  if (!value.startsWith("/") || value.startsWith("//")) return "/dashboard";
  return value;
}
