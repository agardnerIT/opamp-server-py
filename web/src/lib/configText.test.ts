import { describe, expect, it } from "vitest";
import { configObjects } from "./configText";

describe("configObjects", () => {
  it("reads the MessageToDict double-wrapped configMap", () => {
    const objects = configObjects({
      configMap: { configMap: { "": { body: "receivers: {}\n", contentType: "text/yaml" } } },
    });
    expect(objects).toEqual([{ key: "", body: "receivers: {}\n", contentType: "text/yaml" }]);
  });

  it("sorts the empty key first, then the rest by name", () => {
    const objects = configObjects({
      configMap: {
        configMap: {
          "collector.yaml": { body: "b", contentType: "text/yaml" },
          "": { body: "a", contentType: "text/yaml" },
        },
      },
    });
    expect(objects.map((o) => o.key)).toEqual(["", "collector.yaml"]);
  });

  it("accepts a JSON string (some agents report the document as text)", () => {
    const objects = configObjects('{"configMap":{"configMap":{"":{"body":"x","contentType":"text/yaml"}}}}');
    expect(objects).toHaveLength(1);
  });

  it("returns empty for a non-report — a fact, not an error", () => {
    expect(configObjects(null)).toEqual([]);
    expect(configObjects("not json")).toEqual([]);
    expect(configObjects({})).toEqual([]);
  });

  it("keeps `<redacted>` sentinels opaque and untouched", () => {
    const objects = configObjects({
      configMap: { configMap: { "": { body: 'Authorization: "<redacted>"', contentType: "text/yaml" } } },
    });
    expect(objects[0].body).toContain("<redacted>");
  });
});
