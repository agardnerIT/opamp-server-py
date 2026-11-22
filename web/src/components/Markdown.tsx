import { useMemo } from "react";
import { parseMarkdownBlocks, type Inline } from "../lib/markdown";

/**
 * Renders the server's markdown reports (new-ui stage 7). The parsing subset
 * lives in `lib/markdown.ts` (unit-tested); this component only maps blocks
 * to React elements. Text is always a text node — agent-supplied attribute
 * values in a report can never become markup.
 */
function Spans({ spans }: { spans: Inline[] }) {
  return (
    <>
      {spans.map((span, i) =>
        span.kind === "strong" ? (
          <strong key={i}>{span.text}</strong>
        ) : (
          <span key={i}>{span.text}</span>
        ),
      )}
    </>
  );
}

export function Markdown({ text }: { text: string }) {
  const blocks = useMemo(() => parseMarkdownBlocks(text), [text]);
  return (
    <div className="max-w-prose text-[13px] leading-relaxed">
      {blocks.map((block, i) => {
        switch (block.kind) {
          case "h1":
            return (
              <h2 key={i} className="mt-4 mb-1 text-base font-semibold first:mt-0">
                <Spans spans={block.spans} />
              </h2>
            );
          case "h2":
            return (
              <h3 key={i} className="mt-4 mb-1 text-sm font-semibold">
                <Spans spans={block.spans} />
              </h3>
            );
          case "h3":
            return (
              <h4 key={i} className="mt-3 mb-1 text-sm font-medium">
                <Spans spans={block.spans} />
              </h4>
            );
          case "ul":
            return (
              <ul
                key={i}
                className={
                  "my-1.5 list-disc space-y-0.5 " +
                  (block.depth === 1 ? "ml-6 marker:text-[var(--color-muted)]" : "ml-4")
                }
              >
                {block.items.map((item, j) => (
                  <li key={j}>
                    <Spans spans={item} />
                  </li>
                ))}
              </ul>
            );
          case "p":
            return (
              <p key={i} className="my-1.5">
                <Spans spans={block.spans} />
              </p>
            );
        }
      })}
    </div>
  );
}
