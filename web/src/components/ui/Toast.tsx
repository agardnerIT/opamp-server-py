import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { cx } from "./cx";

/**
 * Toast notifications — the one place transient feedback lives, replacing
 * per-page `message` state strings. Success/error/info, auto-dismiss after
 * 5s (errors stay until dismissed — a failure you must read should not
 * evaporate). Persistent-but-inline feedback that belongs next to a field
 * uses `Notice` instead.
 */

export type ToastKind = "ok" | "error" | "warn" | "info";

type ToastEntry = { id: number; kind: ToastKind; text: string };

const ToastContext = createContext<(kind: ToastKind, text: string) => void>(
  () => {},
);

export function useToast() {
  return useContext(ToastContext);
}

const TONE: Record<ToastKind, { dot: string; icon: ReactNode }> = {
  ok: {
    dot: "text-[var(--color-ok)]",
    icon: (
      <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
        <path d="M3 8.5l3.5 3.5L13 4.5" />
      </svg>
    ),
  },
  error: {
    dot: "text-[var(--color-danger)]",
    icon: (
      <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" aria-hidden>
        <circle cx="8" cy="8" r="6.4" />
        <path d="M5.5 5.5l5 5M10.5 5.5l-5 5" />
      </svg>
    ),
  },
  warn: {
    dot: "text-[var(--color-warn)]",
    icon: (
      <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden>
        <path d="M8 2L14.4 13.4H1.6L8 2z" strokeLinejoin="round" />
        <path d="M8 6.6v3" strokeLinecap="round" />
        <circle cx="8" cy="11.4" r="0.7" fill="currentColor" stroke="none" />
      </svg>
    ),
  },
  info: {
    dot: "text-[var(--color-info)]",
    icon: (
      <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden>
        <circle cx="8" cy="8" r="6.4" />
        <path d="M8 7.4v3.6" strokeLinecap="round" />
        <circle cx="8" cy="5.2" r="0.7" fill="currentColor" stroke="none" />
      </svg>
    ),
  },
};

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<ToastEntry[]>([]);
  const nextId = useRef(1);

  const push = useCallback((kind: ToastKind, text: string) => {
    const id = nextId.current++;
    setToasts((list) => [...list, { id, kind, text }]);
    if (kind !== "error") {
      window.setTimeout(() => {
        setToasts((list) => list.filter((t) => t.id !== id));
      }, 5000);
    }
  }, []);

  const value = useMemo(() => push, [push]);

  return (
    <ToastContext.Provider value={value}>
      {children}
      <div
        aria-label="Notifications"
        className="fixed bottom-4 right-4 z-50 flex w-80 flex-col gap-2"
      >
        {toasts.map((t) => (
          <div
            key={t.id}
            role={t.kind === "error" ? "alert" : "status"}
            className={cx(
              "card flex items-start gap-2.5 p-3 pr-2.5 text-[13px] shadow-[var(--shadow-overlay)]",
              "animate-[toast-in_var(--duration-enter)_var(--ease-out)]",
            )}
          >
            <span aria-hidden className={cx("mt-0.5 shrink-0", TONE[t.kind].dot)}>
              {TONE[t.kind].icon}
            </span>
            <span className="min-w-0 break-words">{t.text}</span>
            <button
              type="button"
              aria-label="Dismiss notification"
              onClick={() => setToasts((list) => list.filter((x) => x.id !== t.id))}
              className="ml-auto shrink-0 rounded-[var(--radius-sm)] p-1 text-[var(--color-faint)] hover:text-[var(--color-text)]"
            >
              <svg width="10" height="10" viewBox="0 0 10 10" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" aria-hidden>
                <path d="M1 1l8 8M9 1l-8 8" />
              </svg>
            </button>
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}

/**
 * Inline notice — persistent feedback rendered where the user is looking
 * (inside forms, under section headers). `kind` maps to the tone tokens.
 */
export function Notice({
  kind = "info",
  children,
  className,
}: {
  kind?: ToastKind;
  children: ReactNode;
  className?: string;
}) {
  return (
    <div
      role={kind === "error" ? "alert" : "status"}
      className={cx(
        "flex items-start gap-2 rounded-[var(--radius-md)] border p-2.5 text-[13px] leading-relaxed",
        kind === "ok" &&
          "border-[var(--color-ok)]/30 bg-[var(--color-ok-soft)] text-[var(--color-text)]",
        kind === "error" &&
          "border-[var(--color-danger)]/30 bg-[var(--color-danger-soft)] text-[var(--color-text)]",
        kind === "warn" &&
          "border-[var(--color-warn)]/30 bg-[var(--color-warn-soft)] text-[var(--color-text)]",
        kind === "info" &&
          "border-[var(--color-border)] bg-[var(--color-surface-2)] text-[var(--color-muted)]",
        className,
      )}
    >
      <span aria-hidden className={cx("mt-0.5 shrink-0", TONE[kind].dot)}>
        {TONE[kind].icon}
      </span>
      <div className="min-w-0">{children}</div>
    </div>
  );
}
