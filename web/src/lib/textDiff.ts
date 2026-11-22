/**
 * Minimal line diff for the propose-review step ("diff against current").
 * LCS-based, O(n·m) on lines — config bodies are small (the server caps them),
 * so no histogram/SkipList machinery is warranted.
 */

export interface DiffRow {
  kind: "same" | "add" | "del";
  text: string;
  /** Line number in the old (1-based) or new document, where applicable. */
  line: number;
}

/** The longest common subsequence table for two line arrays. */
function lcsTable(a: string[], b: string[]): Uint32Array {
  const rows = a.length + 1;
  const cols = b.length + 1;
  // Deliberately un-initialised: 0 fill is what we want, and a fresh
  // Uint32Array is zero-filled.
  const table = new Uint32Array(rows * cols);
  for (let i = a.length - 1; i >= 0; i--) {
    for (let j = b.length - 1; j >= 0; j--) {
      table[i * cols + j] =
        a[i] === b[j]
          ? table[(i + 1) * cols + j + 1] + 1
          : Math.max(table[(i + 1) * cols + j], table[i * cols + j + 1]);
    }
  }
  return table;
}

/**
 * Unified row list old → new: deletions and additions kept adjacent (LCS
 * order), context lines carrying both line numbers' old value. `line` counts
 * the document the row exists in (old for same/del, new for add).
 */
export function diffLines(oldText: string, newText: string): DiffRow[] {
  // An empty baseline (agent reported nothing) is "everything added", not
  // "one empty line removed" — `"".split("\n")` would say otherwise.
  if (oldText === "") {
    return newText.split("\n").map((text, idx) => ({ kind: "add" as const, text, line: idx + 1 }));
  }
  if (newText === "") {
    return oldText.split("\n").map((text, idx) => ({ kind: "del" as const, text, line: idx + 1 }));
  }
  const a = oldText.split("\n");
  const b = newText.split("\n");
  const cols = b.length + 1;
  const table = lcsTable(a, b);
  const rows: DiffRow[] = [];
  let i = 0;
  let j = 0;
  while (i < a.length && j < b.length) {
    if (a[i] === b[j]) {
      rows.push({ kind: "same", text: a[i], line: i + 1 });
      i++;
      j++;
    } else if (table[(i + 1) * cols + j] >= table[i * cols + j + 1]) {
      rows.push({ kind: "del", text: a[i], line: i + 1 });
      i++;
    } else {
      rows.push({ kind: "add", text: b[j], line: j + 1 });
      j++;
    }
  }
  while (i < a.length) rows.push({ kind: "del", text: a[i++], line: i });
  while (j < b.length) rows.push({ kind: "add", text: b[j++], line: j });
  return rows;
}

/** Counts by kind, for the one-line "diff summary" the review step shows. */
export function diffSummary(rows: DiffRow[]): { added: number; removed: number } {
  let added = 0;
  let removed = 0;
  for (const row of rows) {
    if (row.kind === "add") added++;
    else if (row.kind === "del") removed++;
  }
  return { added, removed };
}
