import { describe, expect, it } from "vitest";
import {
  absoluteWhen,
  heartbeatAge,
  heartbeatAgeCompact,
  heartbeatTime,
  secondsSince,
} from "./heartbeat";

/** Fixed clock: 2026-11-22T20:00:00Z. Every expectation is relative to this. */
const NOW = Date.parse("2026-11-22T20:00:00Z");
const at = (secondsAgo: number) => new Date(NOW - secondsAgo * 1000).toISOString();

describe("secondsSince", () => {
  it("counts seconds, rounding to the nearest", () => {
    expect(secondsSince(at(12), NOW)).toBe(12);
    expect(secondsSince(at(0.6), NOW)).toBe(1);
  });

  it("clamps a future timestamp to zero instead of going negative", () => {
    // Clock skew between the agent and the browser is real; "-3s ago" is not.
    expect(secondsSince(at(-30), NOW)).toBe(0);
  });

  it("returns null for nothing or a non-date", () => {
    expect(secondsSince(null, NOW)).toBeNull();
    expect(secondsSince(undefined, NOW)).toBeNull();
    expect(secondsSince("not-a-date", NOW)).toBeNull();
  });
});

describe("heartbeatAge", () => {
  it("says just now for the seconds bucket", () => {
    expect(heartbeatAge(at(0), NOW)).toBe("just now");
    expect(heartbeatAge(at(4), NOW)).toBe("just now");
  });

  it("uses the coarsest honest unit", () => {
    expect(heartbeatAge(at(5), NOW)).toBe("5s ago");
    expect(heartbeatAge(at(59), NOW)).toBe("59s ago");
    expect(heartbeatAge(at(60), NOW)).toBe("1m ago");
    expect(heartbeatAge(at(4 * 60 + 30), NOW)).toBe("4m ago");
    expect(heartbeatAge(at(3_600), NOW)).toBe("1h ago");
    expect(heartbeatAge(at(86_399), NOW)).toBe("23h ago");
    expect(heartbeatAge(at(86_400), NOW)).toBe("1d ago");
    expect(heartbeatAge(at(6 * 86_400), NOW)).toBe("6d ago");
  });

  it("switches to a date once '62d ago' would be less useful", () => {
    const age = heartbeatAge(at(30 * 86_400), NOW);
    expect(age).not.toMatch(/ago|just now/);
    expect(new Date(at(30 * 86_400)).toLocaleDateString()).toBe(age);
  });

  it("never renders an empty or negative age", () => {
    expect(heartbeatAge(at(-10), NOW)).toBe("just now");
    expect(heartbeatAge(null, NOW)).toBe("—");
    expect(heartbeatAge("garbage", NOW)).toBe("—");
  });

  it("is monotonic as the clock moves forward between polls", () => {
    // The same heartbeat, read 10s later, can only be older.
    const iso = at(20);
    expect(secondsSince(iso, NOW + 10_000)).toBe(30);
    expect(secondsSince(iso, NOW + 10_000)!).toBeGreaterThan(secondsSince(iso, NOW)!);
  });
});

describe("heartbeatAgeCompact", () => {
  it("drops the words for the fleet chips", () => {
    expect(heartbeatAgeCompact(at(2), NOW)).toBe("now");
    expect(heartbeatAgeCompact(at(12), NOW)).toBe("12s");
    expect(heartbeatAgeCompact(at(240), NOW)).toBe("4m");
    expect(heartbeatAgeCompact(at(7_200), NOW)).toBe("2h");
    expect(heartbeatAgeCompact(at(2 * 86_400), NOW)).toBe("2d");
    expect(heartbeatAgeCompact(null, NOW)).toBe("—");
  });
});

describe("absoluteWhen", () => {
  it("renders the full timestamp for a tooltip", () => {
    expect(absoluteWhen(at(90))).toBe(new Date(Date.parse(at(90))).toLocaleString());
  });

  it("says 'never' for a missing heartbeat and passes junk through", () => {
    expect(absoluteWhen(null)).toBe("never");
    expect(absoluteWhen("2026-13-45")).toBe("2026-13-45");
  });
});

describe("heartbeatTime", () => {
  it("parses an ISO timestamp and rejects junk", () => {
    expect(heartbeatTime("2026-11-22T20:00:00Z")).toBe(NOW);
    expect(heartbeatTime("")).toBeNull();
    expect(heartbeatTime("2026-13-45")).toBeNull();
  });
});
