/**
 * Minimal markdown renderer for the server's report output (new-ui stage 7).
 *
 * The reports (`server/reports.py`) use exactly: `#`/`##`/`###` headings, `- `
 * / `  - ` bullets, `**bold**` spans and plain paragraphs. Rendering the full
 * markdown spec would need a dependency; rendering this subset correctly,
 * dependency-free, is a hundred lines — and stays XSS-safe by construction:
 * every token becomes a React text node, nothing is ever `dangerouslySetInnerHTML`
 * (report content includes agent-supplied attribute values, so a malicious
 * agent must not be able to script the console).
 *
 * Unit-tested in `markdown.test.ts` against the shapes the server emits.
 */

export type Inline =
  | { kind: "text"; text: string }
  | { kind: "strong"; text: string };

/** Split one line into plain / `**bold**` spans. Unclosed `**` stays literal. */
export function parseInline(line: string): Inline[] {
  const spans: Inline[] = [];
  let rest = line;
  for (;;) {
    const start = rest.indexOf("**");
    if (start === -1) break;
    const end = rest.indexOf("**", start + 2);
    if (end === -1) break;
    if (start > 0) spans.push({ kind: "text", text: rest.slice(0, start) });
    spans.push({ kind: "strong", text: rest.slice(start + 2, end) });
    rest = rest.slice(end + 2);
  }
  if (rest) spans.push({ kind: "text", text: rest });
  return spans;
}

export type Block =
  | { kind: "h1" | "h2" | "h3"; spans: Inline[] }
  | { kind: "p"; spans: Inline[] }
  | { kind: "ul"; items: Inline[][]; depth: 0 | 1 };

/**
 * Parse the report subset into blocks. Consecutive `- ` / `  - ` lines group
 * into one list (nested items keep their parent context by list grouping);
 * everything else is a heading or paragraph.
 */
export function parseMarkdownBlocks(markdown: string): Block[] {
  const blocks: Block[] = [];
  let list: Extract<Block, { kind: "ul" }> | null = null;

  const flushList = () => {
    if (list) {
      blocks.push(list);
      list = null;
    }
  };

  for (const rawLine of markdown.split("\n")) {
    const line = rawLine.replace(/\s+$/, "");
    if (line === "") {
      flushList();
      continue;
    }
    const heading = /^(#{1,3})\s+(.*)$/.exec(line);
    if (heading) {
      flushList();
      const depth = heading[1].length;
      blocks.push({
        kind: (depth === 1 ? "h1" : depth === 2 ? "h2" : "h3") as "h1" | "h2" | "h3",
        spans: parseInline(heading[2]),
      });
      continue;
    }
    const bullet = /^(\s*)-\s+(.*)$/.exec(line);
    if (bullet) {
      const depth = bullet[1].length >= 2 ? 1 : 0;
      if (list && list.depth === depth) {
        list.items.push(parseInline(bullet[2]));
      } else {
        flushList();
        list = { kind: "ul", items: [parseInline(bullet[2])], depth };
      }
      continue;
    }
    flushList();
    blocks.push({ kind: "p", spans: parseInline(line) });
  }
  flushList();
  return blocks;
}
