/**
 * Pure keyboard logic for the Tabs primitive (roving tabindex). Exported so
 * it stays unit-testable without a DOM.
 */
export type TabKey = "ArrowLeft" | "ArrowRight" | "Home" | "End";

export function nextTabIndex(
  key: string,
  index: number,
  count: number,
): number | null {
  if (count === 0) return null;
  switch (key) {
    case "ArrowLeft":
      return (index - 1 + count) % count;
    case "ArrowRight":
      return (index + 1) % count;
    case "Home":
      return 0;
    case "End":
      return count - 1;
    default:
      return null;
  }
}
