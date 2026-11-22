import { useId, useRef } from "react";
import { nextTabIndex } from "../../lib/tabKeys";
import { cx } from "./cx";

/**
 * Segmented-control tabs (ARIA tabs pattern, roving tabindex). Used for
 * sibling-choice selectors — an event selector on Alerts, sections inside
 * AgentDetail. Not a replacement for navigation; the keyboard logic is the
 * unit-tested `nextTabIndex` from `lib/tabKeys`.
 */
export function Tabs({
  tabs,
  active,
  onChange,
  ariaLabel,
  className,
}: {
  tabs: { id: string; label: string; count?: number }[];
  active: string;
  onChange: (id: string) => void;
  ariaLabel: string;
  className?: string;
}) {
  const baseId = useId();
  const listRef = useRef<HTMLDivElement>(null);

  function onKeyDown(e: React.KeyboardEvent) {
    const index = tabs.findIndex((t) => t.id === active);
    const next = nextTabIndex(e.key, index < 0 ? 0 : index, tabs.length);
    if (next === null) return;
    e.preventDefault();
    onChange(tabs[next].id);
    // Move focus with the selection (roving tabindex).
    const el = listRef.current?.querySelector<HTMLButtonElement>(
      `#${CSS.escape(`${baseId}-${tabs[next].id}`)}`,
    );
    el?.focus();
  }

  return (
    <div
      ref={listRef}
      role="tablist"
      aria-label={ariaLabel}
      onKeyDown={onKeyDown}
      className={cx(
        "inline-flex items-center gap-0.5 rounded-[var(--radius-md)] border border-[var(--color-border)] bg-[var(--color-surface-2)] p-0.5",
        className,
      )}
    >
      {tabs.map((tab) => {
        const selected = tab.id === active;
        return (
          <button
            key={tab.id}
            id={`${baseId}-${tab.id}`}
            role="tab"
            type="button"
            aria-selected={selected}
            tabIndex={selected ? 0 : -1}
            onClick={() => onChange(tab.id)}
            className={cx(
              "inline-flex h-7 items-center gap-1.5 rounded-[calc(var(--radius-md)-3px)] px-3 text-[13px] font-medium",
              "transition-colors duration-[var(--duration-fast)] ease-[var(--ease-out)]",
              selected
                ? "bg-[var(--color-surface)] text-[var(--color-text)] shadow-[var(--inner-highlight)]"
                : "text-[var(--color-muted)] hover:text-[var(--color-text)]",
            )}
          >
            {tab.label}
            {tab.count !== undefined && (
              <span
                className={cx(
                  "tnum text-xs",
                  selected ? "text-[var(--color-muted)]" : "text-[var(--color-faint)]",
                )}
              >
                {tab.count}
              </span>
            )}
          </button>
        );
      })}
    </div>
  );
}
