/**
 * Display-only decoding of an agent's `effective_config` (stage 5; editing and
 * proposal is stage 6). The shape mirrors `server/effective_config._config_map`:
 * `MessageToDict` produces `{"configMap": {"configMap": {key: {body, contentType}}}}`
 * — the outer wrapper is the report, the inner is the spec's config map.
 *
 * `<redacted>` sentinels pass through as opaque text and are never stripped or
 * rewritten here: the sentinel means "keep the stored value", and echoing it
 * back on a save is how fleet credentials get wiped (plan rule 5).
 */

export interface ConfigObject {
  key: string;
  body: string;
  contentType: string | null;
}

const BASE64_RE = /^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/;

/**
 * Decode an OpAMP `body` (a protobuf `bytes` field, so `MessageToDict` reports
 * base64) to text. Mirrors `server/redaction.decode_text_body`'s conservative
 * guards: length, padding, a strict alphabet and a printable ratio, so a plain
 * YAML/JSON string that merely looks base64-shaped is never re-interpreted.
 * Returns null when the value is not plausibly base64 text.
 */
function decodeBody(value: string): string | null {
  if (value.length < 8 || value.length % 4 !== 0 || !BASE64_RE.test(value)) return null;
  let text: string;
  try {
    const binary = atob(value);
    const bytes = Uint8Array.from(binary, (char) => char.charCodeAt(0));
    text = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
  } catch {
    return null;
  }
  if (!text) return null;
  let printable = 0;
  for (const char of text) {
    if (char >= " " || char === "\n" || char === "\r" || char === "\t") printable += 1;
  }
  return printable / text.length < 0.95 ? null : text;
}

function entryBody(entry: Record<string, unknown>): string | null {
  const body = entry.body;
  if (typeof body !== "string") return null;
  // Prefill and the diff both read this: handing back the base64 blob is the
  // "loaded from agent" bug where the editor showed `Y29ubmVjdG9yczo...`.
  return decodeBody(body) ?? body;
}

function entryField(entry: Record<string, unknown>, name: string): string | null {
  const camel = name.split("_")[0] + name.split("_").slice(1).map((p) => p[0].toUpperCase() + p.slice(1)).join("");
  for (const key of [name, camel]) {
    const v = entry[key];
    if (typeof v === "string") return v;
  }
  return null;
}

/**
 * The config objects an agent reported, `""` first (the spec's conventional
 * "the" object), then the rest by name. Empty when the agent reported nothing
 * parseable — a fact, not an error.
 */
export function configObjects(effectiveConfig: unknown): ConfigObject[] {
  let document: unknown = effectiveConfig;
  if (typeof document === "string") {
    try {
      document = JSON.parse(document);
    } catch {
      return [];
    }
  }
  if (!document || typeof document !== "object") return [];
  const outer = (document as Record<string, unknown>).configMap;
  if (!outer || typeof outer !== "object") return [];
  const inner = (outer as Record<string, unknown>).configMap;
  const map = inner && typeof inner === "object" ? inner : outer;
  const result: ConfigObject[] = [];
  for (const [key, value] of Object.entries(map as Record<string, unknown>)) {
    if (!value || typeof value !== "object") continue;
    const body = entryBody(value as Record<string, unknown>);
    if (body === null) continue;
    result.push({
      key,
      body,
      contentType: entryField(value as Record<string, unknown>, "content_type"),
    });
  }
  return result.sort((a, b) => (a.key === "" ? -1 : b.key === "" ? 1 : a.key.localeCompare(b.key)));
}
