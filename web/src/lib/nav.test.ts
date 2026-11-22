import { describe, expect, it } from "vitest";
import {
  ADMIN_NAV,
  MAIN_NAV,
  ADMIN_SECTION_MIN_ROLE,
  visibleNav,
} from "./nav";
import { roleAtLeast } from "./roles";

describe("roleAtLeast", () => {
  it("follows the server ladder viewer < operator < approver < admin", () => {
    expect(roleAtLeast("viewer", "viewer")).toBe(true);
    expect(roleAtLeast("viewer", "operator")).toBe(false);
    expect(roleAtLeast("operator", "viewer")).toBe(true);
    expect(roleAtLeast("operator", "operator")).toBe(true);
    expect(roleAtLeast("operator", "approver")).toBe(false);
    expect(roleAtLeast("approver", "operator")).toBe(true);
    expect(roleAtLeast("admin", "admin")).toBe(true);
    expect(roleAtLeast("admin", "approver")).toBe(true);
  });

  it("rejects unknown, empty and missing roles", () => {
    expect(roleAtLeast("", "viewer")).toBe(false);
    expect(roleAtLeast("superuser", "viewer")).toBe(false);
    expect(roleAtLeast(null, "viewer")).toBe(false);
    expect(roleAtLeast(undefined, "viewer")).toBe(false);
  });
});

describe("visibleNav", () => {
  it("shows the full Admin section to an admin", () => {
    expect(visibleNav(ADMIN_NAV, "admin")).toHaveLength(ADMIN_NAV.length);
  });

  it("hides admin-only pages (agent tokens, sessions) from an operator", () => {
    const items = visibleNav(ADMIN_NAV, "operator");
    const labels = items.map((i) => i.label);
    expect(labels).toContain("Alerts");
    expect(labels).toContain("Updates");
    expect(labels).not.toContain("Agent tokens");
    expect(labels).not.toContain("Sessions");
  });

  it("shows nothing to a viewer, unknown role or signed-out state", () => {
    expect(visibleNav(ADMIN_NAV, "viewer")).toEqual([]);
    expect(visibleNav(ADMIN_NAV, "superuser")).toEqual([]);
    expect(visibleNav(ADMIN_NAV, null)).toEqual([]);
    expect(visibleNav(ADMIN_NAV, undefined)).toEqual([]);
  });

  it("gates the whole Admin section at its least-privileged page (operator)", () => {
    expect(roleAtLeast("viewer", ADMIN_SECTION_MIN_ROLE)).toBe(false);
    expect(roleAtLeast("operator", ADMIN_SECTION_MIN_ROLE)).toBe(true);
  });

  it("main nav is visible to every role (no minRole entries)", () => {
    expect(MAIN_NAV.every((i) => i.minRole === undefined)).toBe(true);
    // 2027-ui Stage B: `/` is the Overview dashboard (the owner's "generic
    // landing page" ask) and the agents table moved to `/agents`.
    expect(MAIN_NAV.map((i) => i.to)).toEqual([
      "/",
      "/agents",
      "/reports",
      "/groups",
      "/remote-config",
      "/help",
    ]);
  });

  it("admin nav matches ui/app.py's Admin tree in order", () => {
    expect(visibleNav(ADMIN_NAV, "admin").map((i) => i.to)).toEqual([
      "/alerts",
      "/compliance",
      "/agent-tokens",
      "/sessions",
      "/updates",
    ]);
  });
});
