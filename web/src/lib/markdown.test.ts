import { describe, expect, it } from "vitest";
import { parseInline, parseMarkdownBlocks } from "./markdown";

/** The subset `server/reports.py` emits — plus the XSS cases it must never
 * turn into markup (agent-supplied attribute values reach the report). */
describe("parseInline", () => {
  it("splits bold spans", () => {
    expect(parseInline("a **b** c")).toEqual([
      { kind: "text", text: "a " },
      { kind: "strong", text: "b" },
      { kind: "text", text: " c" },
    ]);
  });

  it("keeps unclosed ** literal", () => {
    expect(parseInline("a ** b")).toEqual([{ kind: "text", text: "a ** b" }]);
  });
});

describe("parseMarkdownBlocks", () => {
  it("parses headings, bullets and paragraphs", () => {
    const blocks = parseMarkdownBlocks(
      "# Agent Report\n\nTotal Agents: 2\n\n## Component Versions\n- **0.149.0**: 5 components\n  - nested\n",
    );
    expect(blocks).toEqual([
      { kind: "h1", spans: [{ kind: "text", text: "Agent Report" }] },
      { kind: "p", spans: [{ kind: "text", text: "Total Agents: 2" }] },
      { kind: "h2", spans: [{ kind: "text", text: "Component Versions" }] },
      {
        kind: "ul",
        depth: 0,
        items: [[{ kind: "strong", text: "0.149.0" }, { kind: "text", text: ": 5 components" }]],
      },
      { kind: "ul", depth: 1, items: [[{ kind: "text", text: "nested" }]] },
    ]);
  });

  it("never produces raw HTML — agent-supplied markup stays text", () => {
    const blocks = parseMarkdownBlocks("- <script>alert(1)</script>\n![x](javascript:alert(1))");
    const rendered = JSON.stringify(blocks);
    expect(rendered).toContain("<script>alert(1)</script>");
    expect(rendered).not.toContain('"html"');
  });
});
