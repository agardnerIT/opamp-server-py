/**
 * Which reports the Reports page offers (new-ui stage 7), mirroring
 * `ui/pages/2_Reports.py::_offered_reports`: the union of what the fleet's
 * profiles declare, in a stable readable order — never the order agents
 * happened to arrive in, and never a report whose concept no agent has.
 *
 * `fleet` is always offered: it describes the server's own view, which exists
 * for every profile.
 */

import type { AgentTypeSummary } from "../api/endpoints";

export type ReportKind = "fleet" | "heavy" | "outdated";

/** Neutral wording on purpose (#121): "Heavy Collectors" was a page literal
 * that described one profile's report as if it were a fact about every agent. */
export const REPORT_LABELS: Record<ReportKind, string> = {
  fleet: "Fleet summary",
  heavy: "Heavy agents (>50% unused components)",
  outdated: "Outdated components",
};

/** Stable, readable order: fleet, heavy, outdated. */
const ORDER: ReportKind[] = ["fleet", "heavy", "outdated"];

export function offeredReports(types: AgentTypeSummary[] | undefined): ReportKind[] {
  const declared = new Set<ReportKind>(["fleet"]);
  for (const profile of types ?? []) {
    for (const report of profile.reports) {
      if (report === "heavy" || report === "outdated") declared.add(report);
    }
  }
  return ORDER.filter((kind) => declared.has(kind));
}
