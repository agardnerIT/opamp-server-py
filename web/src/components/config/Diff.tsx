import { diffLines, diffSummary, type DiffRow } from "../../lib/textDiff";

const ROW_CLASS: Record<DiffRow["kind"], string> = {
  same: "",
  add: "bg-[color-mix(in_srgb,var(--color-ok)_15%,transparent)]",
  del: "bg-[color-mix(in_srgb,var(--color-danger)_15%,transparent)] text-[var(--color-muted)] line-through decoration-[var(--color-danger)]/60",
};

/**
 * Proposed-body vs current-config review diff. Line-based, no context
 * collapsing — config bodies are small enough to read whole, and elided
 * context invites approving a change nobody looked at.
 */
export function Diff({ oldText, newText }: { oldText: string; newText: string }) {
  const rows = diffLines(oldText, newText);
  const { added, removed } = diffSummary(rows);
  if (added === 0 && removed === 0) {
    return <p className="text-xs text-[var(--color-muted)]">No difference from the current config.</p>;
  }
  return (
    <div className="card-2 overflow-x-auto p-2.5 font-mono text-xs leading-5">
      <div className="mb-1 text-[11px] text-[var(--color-muted)]">
        +{added} / −{removed} lines
      </div>
      {rows.map((row, i) => (
        <div key={i} className={`flex ${ROW_CLASS[row.kind]}`}>
          <span className="w-10 shrink-0 select-none text-right text-[var(--color-muted)]">
            {row.kind === "add" ? "+" : row.kind === "del" ? "−" : ""}
          </span>
          <span className="whitespace-pre-wrap break-all">{row.text || " "}</span>
        </div>
      ))}
    </div>
  );
}
