import { describe, expect, it } from "vitest";
import {
  connectorPath,
  MAX_FLEET_NODES,
  nodeKey,
  topologySummary,
} from "./topology";

describe("connectorPath", () => {
  it("starts and ends exactly on the two node edges", () => {
    const d = connectorPath({ x: 168, y: 84 }, { x: 520, y: 22 });
    expect(d.startsWith("M 168 84 ")).toBe(true);
    expect(d.endsWith("520 22")).toBe(true);
    expect(d).toContain("C");
  });

  it("bends horizontally by 45% of the gap", () => {
    // gap 200 → bend 90; both control points sit 90px inside the gap.
    expect(connectorPath({ x: 0, y: 0 }, { x: 200, y: 40 })).toBe(
      "M 0 0 C 90 0, 110 40, 200 40",
    );
  });

  it("clamps the bend so tiny gaps stay shallow", () => {
    // gap 10 → 45% = 4.5, under the 16px floor.
    expect(connectorPath({ x: 0, y: 0 }, { x: 10, y: 0 })).toBe(
      "M 0 0 C 16 0, -6 0, 10 0",
    );
  });

  it("clamps the bend so wide gaps cannot bulge out of the card", () => {
    // gap 1000 → 450, capped at 96.
    expect(connectorPath({ x: 0, y: 0 }, { x: 1000, y: 0 })).toBe(
      "M 0 0 C 96 0, 904 0, 1000 0",
    );
  });

  it("rounds to one decimal so the path string is stable", () => {
    expect(connectorPath({ x: 10.04, y: 2.34567 }, { x: 110.98, y: 3.4 })).toBe(
      "M 10 2.3 C 55.5 2.3, 65.6 3.4, 111 3.4",
    );
  });
});

describe("topologySummary", () => {
  it("names the empty state as listening, not zero", () => {
    expect(topologySummary([])).toBe(
      "Diagram: the OpAMP server is listening, with no agents connected.",
    );
  });

  it("counts the fleet and the healthy subset", () => {
    expect(topologySummary([{ healthy: true }, { healthy: false }])).toBe(
      "Diagram: 2 agents connected to the server, 1 reporting healthy.",
    );
  });

  it("does not say '1 agents'", () => {
    expect(topologySummary([{ healthy: null }])).toBe(
      "Diagram: 1 agent connected to the server, 0 reporting healthy.",
    );
  });
});

describe("nodeKey", () => {
  it("changes when the measured node set changes", () => {
    expect(nodeKey([{ id: "a" }, { id: "b" }])).toBe("a|b");
    expect(nodeKey([{ id: "a" }, { id: "b" }])).not.toBe(nodeKey([{ id: "a" }]));
  });

  it("draws at most MAX_FLEET_NODES before collapsing the tail", () => {
    expect(MAX_FLEET_NODES).toBe(5);
  });
});
