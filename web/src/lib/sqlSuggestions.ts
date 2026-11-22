import type { QuerySchemaColumn } from "../api/endpoints";

/**
 * Suggestions for the one-box SQL search (#132 parity, `ui/sql_search.py`).
 * The rule that makes a suggestion honest: a value is offered only because a
 * live agent reported it — the schema endpoint already samples the fleet.
 */

export interface Suggestion {
  /** Text to insert when accepted (a `sql_name` or a quoted literal). */
  insert: string;
  /** What it is, shown in the popup. */
  kind: "column" | "value";
  detail?: string;
}

/** The word currently being typed (bare or quoted), ending at `text.length`. */
function currentToken(text: string): string {
  const m = /(?:^|\s)([A-Za-z0-9_."]+)$/.exec(text);
  return m ? m[1] : "";
}

function quoteLiteral(value: string): string {
  return `'${value.replaceAll("'", "''")}'`;
}

/**
 * Pure helper the search box calls on every keystroke. Context beats a flat
 * list: after `column = ` the popup offers that column's reported values,
 * otherwise it completes column names against the token.
 */
export function sqlSuggestions(
  columns: QuerySchemaColumn[],
  text: string,
  limit = 8,
): Suggestion[] {
  const token = currentToken(text);
  const beforeToken = token ? text.slice(0, text.length - token.length) : text;

  // After `col = ` (or `col ==`/`col LIKE`) suggest the values that column's
  // agents reported. The column may still be mid-typing, so try the bare word.
  const valueContext = /([A-Za-z0-9_."]+)\s*=\s*$/.exec(beforeToken);
  if (valueContext) {
    const raw = valueContext[1].replaceAll('"', "");
    const column = columns.find((c) => c.name === raw || c.sql_name === raw);
    // A known column with nothing reported offers nothing: a suggestion must
    // be a value the fleet actually reports, not a guess.
    if (column && column.values.length === 0) return [];
    if (column) {
      const filter = token.toLowerCase();
      return column.values
        .map((v) => String(v))
        .filter((v) => v.toLowerCase().includes(filter))
        .slice(0, limit)
        .map((v) => ({ insert: quoteLiteral(v), kind: "value" as const }));
    }
  }

  // Complete column names. `sql_name` is the server's quoting decision — a
  // dotted attribute inserts as `"service.name"`, which is what SQLite needs.
  const lower = token.toLowerCase().replaceAll('"', "");
  const result: Suggestion[] = [];
  for (const column of columns) {
    if (lower && !column.name.toLowerCase().includes(lower)) continue;
    result.push({
      insert: column.sql_name,
      kind: "column",
      detail: column.kind === "attribute" ? "attribute" : column.kind,
    });
    if (result.length >= limit) break;
  }
  return result;
}
