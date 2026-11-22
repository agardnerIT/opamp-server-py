import { describe, expect, it } from "vitest";
import { offeredReports } from "./reportTypes";
import type { AgentTypeSummary } from "../api/endpoints";

function profile(name: string, reports: string[]): AgentTypeSummary {
  return {
    name,
    display_name: name,
    icon: "📦",
    priority: 1,
    summary: "",
    guidance: "",
    validates_structure: false,
    content_types: [],
    default_filename: "config",
    reports,
    telemetry: null,
    detectable: false,
    has_components: false,
    has_onboarding_snippet: false,
    panels: [],
    bootstrap_default: false,
  };
}

describe("offeredReports", () => {
  it("always offers fleet, never a concept no profile declares", () => {
    expect(offeredReports([profile("generic", [])])).toEqual(["fleet"]);
  });

  it("is the union of what the fleet's profiles declare, in stable order", () => {
    expect(offeredReports([profile("generic", []), profile("otelcol", ["outdated", "fleet", "heavy", "agent"])])).toEqual([
      "fleet",
      "heavy",
      "outdated",
    ]);
  });

  it("offers nothing unknown and tolerates no fleet", () => {
    expect(offeredReports([profile("odd", ["nonsense"])]).join()).toBe("fleet");
    expect(offeredReports(undefined)).toEqual(["fleet"]);
  });
});
