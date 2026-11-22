import { describe, expect, it } from "vitest";
import { statusLabel, statusTone } from "./deliveryStatus";

/**
 * The vocabulary is the server's, not ours: every state the server defines
 * (memory.md ~109) must land in the tone the vocabulary means.
 */
describe("statusTone", () => {
  it("maps push-response states", () => {
    expect(statusTone("delivered")).toBe("ok");
    expect(statusTone("queued")).toBe("warn");
    expect(statusTone("unchanged")).toBe("muted");
  });

  it("maps assignment states", () => {
    expect(statusTone("none")).toBe("muted");
    expect(statusTone("applied")).toBe("ok");
    expect(statusTone("failed")).toBe("danger");
    expect(statusTone("poisoned")).toBe("danger");
    expect(statusTone("unconfirmed")).toBe("warn");
  });

  it("maps remote-config (apply verdict) states case-insensitively", () => {
    expect(statusTone("APPLIED")).toBe("ok");
    expect(statusTone("APPLYING")).toBe("warn");
    expect(statusTone("FAILED")).toBe("danger");
    expect(statusTone("UNSET")).toBe("muted");
    expect(statusTone(null)).toBe("muted");
  });

  it("never invents a state: unknown values render muted, not colored", () => {
    expect(statusTone("WEIRD_STATE")).toBe("muted");
    expect(statusTone("")).toBe("muted");
  });
});

describe("statusLabel", () => {
  it("uppercases enum tokens and keeps unknown spellings verbatim", () => {
    expect(statusLabel("applied")).toBe("APPLIED");
    expect(statusLabel("unset")).toBe("Unset");
    expect(statusLabel(null)).toBe("Unknown");
    expect(statusLabel("config_conflict")).toBe("config_conflict");
  });
});
