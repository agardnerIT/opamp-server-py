import { describe, expect, it } from "vitest";
import type { QuerySchemaColumn } from "../api/endpoints";
import { sqlSuggestions } from "./sqlSuggestions";

const columns: QuerySchemaColumn[] = [
  {
    name: "healthy",
    sql_name: "healthy",
    kind: "core",
    description: "",
    values: [1, 0],
    agent_count: 2,
  },
  {
    name: "service.name",
    sql_name: '"service.name"',
    kind: "attribute",
    description: "",
    values: ["collector-prod"],
    agent_count: 1,
  },
  {
    name: "host_arch",
    sql_name: "host_arch",
    kind: "attribute",
    description: "",
    values: ["amd64", "arm64"],
    agent_count: 2,
  },
];

describe("sqlSuggestions", () => {
  it("completes column names from the typed token", () => {
    const s = sqlSuggestions(columns, "SELECT * FROM agents WHERE he");
    expect(s[0]).toMatchObject({ insert: "healthy", kind: "column" });
  });

  it("inserts the quoted spelling for dotted attributes", () => {
    const s = sqlSuggestions(columns, "SELECT * FROM agents WHERE service.n");
    expect(s.map((x) => x.insert)).toContain('"service.name"');
  });

  it("suggests values after `column = `, only values the fleet reported", () => {
    const s = sqlSuggestions(columns, "SELECT * FROM agents WHERE host_arch = ");
    expect(s.map((x) => x.insert)).toEqual(["'amd64'", "'arm64'"]);
    expect(s.every((x) => x.kind === "value")).toBe(true);
  });

  it("filters values against the partially typed literal", () => {
    const s = sqlSuggestions(columns, "SELECT * FROM agents WHERE host_arch = am");
    expect(s.map((x) => x.insert)).toEqual(["'amd64'"]);
  });

  it("an attribute with no reported values offers nothing", () => {
    const empty: QuerySchemaColumn[] = [
      {
        name: "stale",
        sql_name: "stale",
        kind: "core",
        description: "",
        values: [],
        agent_count: 0,
      },
    ];
    const s = sqlSuggestions(empty, "SELECT * FROM agents WHERE stale = ");
    expect(s).toEqual([]);
  });

  it("empty text offers columns to start from", () => {
    const s = sqlSuggestions(columns, "");
    expect(s.length).toBeGreaterThan(0);
    expect(s.every((x) => x.kind === "column")).toBe(true);
  });
});
