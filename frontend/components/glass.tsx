import Link from "next/link";

export function GlassCard({
  children,
  className = "",
  raised = false,
}: {
  children: React.ReactNode;
  className?: string;
  raised?: boolean;
}) {
  return <div className={`${raised ? "glass-raised" : "glass"} ${className}`}>{children}</div>;
}

/** Small caps eyebrow. Used to name a region, never for decoration. */
export function Eyebrow({ children }: { children: React.ReactNode }) {
  return (
    <p className="font-mono text-[11px] font-medium tracking-[0.02em] text-parchment-dim">
      {children}
    </p>
  );
}

export function Shell({
  title,
  eyebrow,
  children,
  action,
  /** Widen the column. Only the tutor uses it: it carries a sidebar beside the
   *  transcript, and at max-w-5xl the two panes fight over the same space. */
  wide = false,
}: {
  title: string;
  eyebrow?: string;
  children: React.ReactNode;
  action?: React.ReactNode;
  wide?: boolean;
}) {
  return (
    <main
      className={`mx-auto w-full flex-1 px-5 py-10 sm:px-8 sm:py-14 ${
        wide ? "max-w-6xl" : "max-w-5xl"
      }`}
    >
      <header className="rise mb-8 flex flex-wrap items-end justify-between gap-4">
        <div className="flex flex-col gap-2">
          {eyebrow && <Eyebrow>{eyebrow}</Eyebrow>}
          <h1 className="font-display text-4xl font-semibold tracking-tight sm:text-[2.75rem]">
            {title}
          </h1>
        </div>
        {action}
      </header>
      {children}
    </main>
  );
}

export function TextLink({ href, children }: { href: string; children: React.ReactNode }) {
  return (
    <Link
      href={href}
      className="text-parchment-dim underline decoration-parchment-dim/30 underline-offset-4 transition hover:text-parchment hover:decoration-parchment/60"
    >
      {children}
    </Link>
  );
}
