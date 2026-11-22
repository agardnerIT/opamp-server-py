import { useEffect, useRef } from "react";

/**
 * Confirm/input dialog, built on the native `<dialog>` element: focus is
 * trapped and Esc works without a dependency (the plan's Radix/shadcn decision
 * deferred until a page needs full dialog primitives — one confirm per
 * destructive step does not justify a dependency tree).
 *
 * Backdrop click does NOT close: these dialogs confirm irreversible actions,
 * and an accidental click outside should not count as a decision.
 */
export function Modal({
  open,
  title,
  onClose,
  children,
}: {
  open: boolean;
  title: string;
  onClose: () => void;
  children: React.ReactNode;
}) {
  const ref = useRef<HTMLDialogElement>(null);

  useEffect(() => {
    const dialog = ref.current;
    if (!dialog) return;
    if (open && !dialog.open) dialog.showModal();
    else if (!open && dialog.open) dialog.close();
  }, [open]);

  // `close` fires for Esc as well; keep React state in step either way.
  return (
    <dialog
      ref={ref}
      aria-labelledby="modal-title"
      onClose={onClose}
      className="m-auto w-[26rem] max-w-[calc(100vw-2rem)] rounded-[var(--radius-lg)] border border-[var(--color-border-strong)] bg-[var(--color-surface)] p-5 text-[var(--color-text)] shadow-[var(--shadow-overlay)] backdrop:bg-black/60"
    >
      <h2 id="modal-title" className="mb-3 text-sm font-semibold">
        {title}
      </h2>
      {children}
    </dialog>
  );
}
