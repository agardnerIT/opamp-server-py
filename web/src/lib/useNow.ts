import { useEffect, useState } from "react";

/**
 * A clock that re-renders on an interval, for relative times ("12s ago").
 *
 * Without it a rendered age freezes at whatever it said when the row was
 * fetched, which is exactly the stale-data problem the age exists to reveal.
 * One interval per component is enough; 10s is below the resolution of
 * everything but the seconds bucket.
 */
export function useNow(intervalMs = 10_000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), intervalMs);
    return () => clearInterval(id);
  }, [intervalMs]);
  return now;
}
