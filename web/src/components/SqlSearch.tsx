import { useId, useMemo, useState } from "react";
import type { QuerySchemaColumn } from "../api/endpoints";
import { sqlSuggestions } from "../lib/sqlSuggestions";

/**
 * The one-box SQL search for the Agents list (#132 parity): one input, a
 * suggestion popup computed from `GET /agents/query/schema`, Tab to accept,
 * Enter to run, Escape to close. Submitting is the only thing that acts —
 * typing never triggers a query.
 *
 * The popup is keyboard-operable with a visible focus stand-in (`aria-activedescendant`
 * + highlight), because the input keeps focus while the popup is open.
 */
export function SqlSearch({
  columns,
  value,
  onSubmit,
  label = "Search agents with SQL",
  placeholder = "SELECT * FROM agents WHERE ...",
}: {
  columns: QuerySchemaColumn[];
  value: string;
  onSubmit: (text: string) => void;
  label?: string;
  placeholder?: string;
}) {
  const [text, setText] = useState(value);
  const [active, setActive] = useState(0);
  const [open, setOpen] = useState(false);
  const inputId = useId();
  const listId = `${inputId}-listbox`;

  const suggestions = useMemo(
    () => (open ? sqlSuggestions(columns, text) : []),
    [open, columns, text],
  );

  function accept(suggestion: { insert: string }) {
    // Replace just the token being typed — everything before it stays.
    const token = /(?:^|\s)([A-Za-z0-9_."]+)$/.exec(text);
    const next = token ? text.slice(0, text.length - token[1].length) + suggestion.insert + " " : text + suggestion.insert;
    setText(next);
    setOpen(true);
  }

  return (
    <div role="search" className="relative">
      <label htmlFor={inputId} className="mb-1 block text-[13px] font-medium">
        {label}
      </label>
      <input
        id={inputId}
        type="text"
        role="combobox"
        aria-expanded={suggestions.length > 0}
        aria-controls={listId}
        aria-autocomplete="list"
        aria-activedescendant={suggestions.length > 0 ? `${listId}-${active}` : undefined}
        autoComplete="off"
        spellCheck={false}
        className="field font-mono text-[13px]"
        placeholder={placeholder}
        value={text}
        onChange={(e) => {
          setText(e.target.value);
          setOpen(true);
          setActive(0);
        }}
        onFocus={() => setOpen(true)}
        onBlur={() => setOpen(false)}
        onKeyDown={(e) => {
          if (e.key === "Enter") {
            e.preventDefault();
            setOpen(false);
            onSubmit(text.trim());
          } else if (e.key === "Tab" && suggestions.length > 0) {
            e.preventDefault();
            accept(suggestions[active] ?? suggestions[0]);
          } else if (e.key === "ArrowDown") {
            e.preventDefault();
            setActive((a) => (suggestions.length ? (a + 1) % suggestions.length : 0));
          } else if (e.key === "ArrowUp") {
            e.preventDefault();
            setActive((a) => (suggestions.length ? (a - 1 + suggestions.length) % suggestions.length : 0));
          } else if (e.key === "Escape") {
            setOpen(false);
          }
        }}
      />
      {suggestions.length > 0 && (
        <ul
          id={listId}
          role="listbox"
          aria-label="SQL suggestions"
          className="card absolute z-10 mt-1 max-h-64 w-full overflow-y-auto p-1 shadow-[var(--shadow-overlay)]"
        >
          {suggestions.map((s, i) => (
            <li
              key={`${s.insert}-${i}`}
              id={`${listId}-${i}`}
              role="option"
              aria-selected={i === active}
              className={
                "flex cursor-pointer items-baseline justify-between gap-3 rounded px-2 py-1.5 font-mono text-xs " +
                (i === active ? "bg-[var(--color-bg)] text-[var(--color-text)]" : "text-[var(--color-muted)]")
              }
              onMouseDown={(e) => {
                // mousedown, not click: the input's blur must not swallow it.
                e.preventDefault();
                accept(s);
              }}
              onMouseEnter={() => setActive(i)}
            >
              <span>{s.insert}</span>
              <span className="shrink-0 font-sans text-[10px] uppercase tracking-wide text-[var(--color-muted)]">
                {s.kind}
              </span>
            </li>
          ))}
        </ul>
      )}
      <p className="mt-1 text-xs text-[var(--color-faint)]">
        Read-only SQL over the live fleet. Description attributes are columns; suggestions
        come from the values this fleet reports. Tab accepts · Enter runs.
      </p>
    </div>
  );
}
