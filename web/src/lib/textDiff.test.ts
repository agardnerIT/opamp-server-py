import { describe, expect, it } from "vitest";
import { diffLines, diffSummary } from "./textDiff";

describe("diffLines", () => {
  it("returns nothing for identical texts", () => {
    expect(diffLines("a\nb", "a\nb")).toHaveLength(2);
    expect(diffSummary(diffLines("a\nb", "a\nb"))).toEqual({ added: 0, removed: 0 });
  });

  it("marks added lines with new numbering", () => {
    const rows = diffLines("a\nc", "a\nb\nc");
    expect(rows.filter((r) => r.kind === "add")).toEqual([{ kind: "add", text: "b", line: 2 }]);
  });

  it("marks removed lines with old numbering", () => {
    const rows = diffLines("a\nb\nc", "a\nc");
    expect(rows.filter((r) => r.kind === "del")).toEqual([{ kind: "del", text: "b", line: 2 }]);
  });

  it("pairs a changed block as del followed by add", () => {
    const rows = diffLines("exporters: [otlp]", "exporters: [debug]");
    expect(diffSummary(rows)).toEqual({ added: 1, removed: 1 });
    expect(rows.map((r) => r.kind)).toEqual(["del", "add"]);
  });

  it("handles empty baselines (no current config)", () => {
    const rows = diffLines("", "new");
    expect(diffSummary(rows)).toEqual({ added: 1, removed: 0 });
  });
});
