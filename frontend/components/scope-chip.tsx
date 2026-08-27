/**
 * The one label that must never be misread.
 *
 * `canon` is a book shared with everyone signed in, and the only pool a test can
 * be generated from. `personal` is a private upload that only its owner can see.
 * Those are different enough that they get different colours rather than different
 * words in the same grey.
 *
 * The wording is deliberately the reader's, not the schema's: "shared" and "private",
 * never "canon" and "personal".
 */
export function ScopeChip({ scope, className = "" }: { scope: string; className?: string }) {
  const canon = scope === "canon";
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 font-mono text-[11px] tracking-wide ${
        canon
          ? "border-canon/40 bg-canon/12 text-canon"
          : "border-personal/40 bg-personal/12 text-personal"
      } ${className}`}
    >
      <span
        aria-hidden
        className={`size-1.5 rounded-full ${canon ? "bg-canon" : "bg-personal"}`}
      />
      {canon ? "shared" : "private"}
    </span>
  );
}
