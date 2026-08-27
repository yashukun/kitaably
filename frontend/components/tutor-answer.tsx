"use client";

import { type Citation } from "@/lib/api/chat";

/**
 * Renders a tutor answer: a small, deliberate subset of markdown, plus the one
 * thing a general markdown library could not do — turn `[1]` into a live chip that
 * resolves to the actual book and page.
 *
 * Written by hand rather than pulled from npm for two reasons. The answer arrives a
 * token at a time, so every function here has to survive *half* a construct — a
 * lone `**`, a `##` with no line after it, a `[` that has not become a `[2]` yet —
 * and produce something sane rather than throwing mid-stream. And a citation is the
 * product, not a footnote: it has to know what it points at.
 *
 * Anything not handled below falls through as plain text. That is the safe
 * direction: an unrendered asterisk is a blemish, whereas a renderer that guesses
 * at partial syntax flickers on every keystroke of the stream.
 */

type Props = { content: string; citations: Citation[]; onOpen?: (index: number) => void };

/** `[1]`, `[2]` — and `[1, 2]`, which models write despite being asked not to. */
const CITE = /\[(\d+(?:\s*,\s*\d+)*)\]/g;

function Chip({
  index,
  citation,
  onOpen,
}: {
  index: number;
  citation: Citation;
  onOpen?: (index: number) => void;
}) {
  const where = `${citation.book_title}${citation.page ? `, page ${citation.page}` : ""}`;

  return (
    <button
      type="button"
      onClick={() => onOpen?.(index)}
      title={where}
      aria-label={`Source ${index + 1}: ${where}`}
      className="mx-0.5 inline-flex h-[18px] min-w-[18px] items-center justify-center
                 rounded-[5px] bg-indigo/25 px-1 align-[1px] font-mono text-[10px]
                 leading-none text-parchment/90 transition hover:bg-indigo/45"
    >
      {index + 1}
    </button>
  );
}

/** Bold, italic and code, then citation chips. Order matters: the chip pass runs
 *  last so a `[1]` inside bold text still becomes a chip. */
function inline(
  text: string,
  citations: Citation[],
  onOpen: ((index: number) => void) | undefined,
  keyPrefix: string,
): React.ReactNode[] {
  const nodes: React.ReactNode[] = [];
  // One regex over all three: whichever alternative matches wins at that position,
  // so `**bold**` and `` `code` `` cannot interleave into nonsense.
  const pattern = /(\*\*[^*]+\*\*|\*[^*\n]+\*|`[^`\n]+`)/g;
  let cursor = 0;
  let match: RegExpExecArray | null;
  let n = 0;

  const plain = (chunk: string, key: string) => {
    // Citation chips are only meaningful in plain runs; a `[1]` inside a code span
    // is a literal and should stay one.
    let last = 0;
    let cite: RegExpExecArray | null;
    CITE.lastIndex = 0;
    while ((cite = CITE.exec(chunk)) !== null) {
      if (cite.index > last) nodes.push(chunk.slice(last, cite.index));
      for (const raw of cite[1].split(",")) {
        const index = Number(raw.trim()) - 1;
        const citation = citations[index];
        if (citation) {
          nodes.push(
            <Chip
              key={`${key}-c${index}-${nodes.length}`}
              index={index}
              citation={citation}
              onOpen={onOpen}
            />,
          );
        }
        // A number with no source behind it is dropped, not rendered as a dead
        // chip. Models do occasionally cite a [4] they were never given, and a
        // clickable control that goes nowhere is worse than a missing marker —
        // the reader has no way to tell it apart from one that works.
      }
      last = cite.index + cite[0].length;
    }
    if (last < chunk.length) nodes.push(chunk.slice(last));
  };

  while ((match = pattern.exec(text)) !== null) {
    if (match.index > cursor) plain(text.slice(cursor, match.index), `${keyPrefix}-p${n}`);
    const token = match[0];
    const key = `${keyPrefix}-m${n++}`;
    if (token.startsWith("**")) {
      nodes.push(
        <strong key={key} className="font-semibold text-parchment">
          {token.slice(2, -2)}
        </strong>,
      );
    } else if (token.startsWith("`")) {
      nodes.push(
        <code
          key={key}
          className="rounded bg-bark/70 px-1 py-0.5 font-mono text-[0.85em] text-parchment/90"
        >
          {token.slice(1, -1)}
        </code>,
      );
    } else {
      nodes.push(
        <em key={key} className="italic">
          {token.slice(1, -1)}
        </em>,
      );
    }
    cursor = match.index + token.length;
  }
  if (cursor < text.length) plain(text.slice(cursor), `${keyPrefix}-p${n}`);
  return nodes;
}

export function TutorAnswer({ content, citations, onOpen }: Props) {
  const blocks: React.ReactNode[] = [];
  const lines = content.split("\n");

  let paragraph: string[] = [];
  let list: { ordered: boolean; items: string[] } | null = null;

  const flushParagraph = () => {
    if (!paragraph.length) return;
    const text = paragraph.join(" ");
    blocks.push(
      <p key={`p${blocks.length}`} className="text-[15px] leading-[1.7] text-parchment/90">
        {inline(text, citations, onOpen, `p${blocks.length}`)}
      </p>,
    );
    paragraph = [];
  };

  const flushList = () => {
    if (!list) return;
    const { ordered, items } = list;
    const Tag = ordered ? "ol" : "ul";
    blocks.push(
      <Tag
        key={`l${blocks.length}`}
        className={`ml-1 flex list-outside flex-col gap-1.5 pl-4 text-[15px] leading-[1.7]
                    text-parchment/90 ${ordered ? "list-decimal" : "list-disc"}`}
      >
        {items.map((item, index) => (
          <li key={index} className="pl-1 marker:text-parchment-dim">
            {inline(item, citations, onOpen, `l${blocks.length}-${index}`)}
          </li>
        ))}
      </Tag>,
    );
    list = null;
  };

  for (const raw of lines) {
    const line = raw.trimEnd();

    if (!line.trim()) {
      flushParagraph();
      flushList();
      continue;
    }

    const heading = /^(#{1,4})\s+(.*)$/.exec(line);
    if (heading) {
      flushParagraph();
      flushList();
      const depth = heading[1].length;
      // The tutor is asked for `## Title` as the first line, so the commonest
      // heading is also the answer's title — hence the largest treatment here
      // rather than a strict h1..h4 ramp nobody would notice.
      blocks.push(
        <h3
          key={`h${blocks.length}`}
          className={
            depth <= 2
              ? "font-display text-[22px] leading-snug text-parchment"
              : "mt-1 font-display text-[17px] leading-snug text-parchment/95"
          }
        >
          {inline(heading[2], citations, onOpen, `h${blocks.length}`)}
        </h3>,
      );
      continue;
    }

    const bullet = /^[-*+]\s+(.*)$/.exec(line);
    const numbered = /^\d+[.)]\s+(.*)$/.exec(line);
    if (bullet || numbered) {
      flushParagraph();
      const ordered = Boolean(numbered);
      if (!list || list.ordered !== ordered) {
        flushList();
        list = { ordered, items: [] };
      }
      list.items.push((bullet ?? numbered)![1]);
      continue;
    }

    // A continuation line inside a list item, indented under it.
    if (list && /^\s{2,}\S/.test(raw)) {
      list.items[list.items.length - 1] += ` ${line.trim()}`;
      continue;
    }

    flushList();
    paragraph.push(line.trim());
  }

  flushParagraph();
  flushList();

  return <div className="flex flex-col gap-3.5">{blocks}</div>;
}
