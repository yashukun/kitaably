"use client";

import { useEffect, useRef } from "react";

/**
 * Fades a block in the first time it scrolls into view. The hidden state lives in
 * CSS behind `@media (scripting: enabled)`, so a browser that never runs this
 * component simply shows the content.
 */
export function Reveal({
  children,
  className = "",
  delay = 0,
}: {
  children: React.ReactNode;
  className?: string;
  /** Milliseconds. Staggers siblings that enter the viewport together. */
  delay?: number;
}) {
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const node = ref.current;
    if (!node) return;
    const observer = new IntersectionObserver(
      ([entry]) => {
        if (entry.isIntersecting) {
          node.dataset.shown = "true";
          observer.disconnect();
        }
      },
      // Fire a little before the block fully arrives, so the motion is in
      // progress as the reader reaches it rather than starting late.
      { threshold: 0.15, rootMargin: "0px 0px -8% 0px" },
    );
    observer.observe(node);
    return () => observer.disconnect();
  }, []);

  return (
    <div
      ref={ref}
      className={`reveal ${className}`}
      style={delay ? { transitionDelay: `${delay}ms` } : undefined}
    >
      {children}
    </div>
  );
}
