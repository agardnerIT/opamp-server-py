/**
 * Error normalization — the ONE place 401/403/429/503 semantics are decided.
 *
 * This is the client half of the contract the server implements (`401` vs
 * `403` vs `429` vs `503` are four different sentences, and conflating any two
 * of them either logs an operator out for a role denial or leaves a dead
 * session on screen):
 *
 * - 401 ⇒ the session is gone; the caller must drop state and show login
 *   (`relogin` is true). Never treated as a permissions problem.
 * - 403 ⇒ session stays; either the role ladder refused the action
 *   (`forbidden`) or the server is in read-only mode with no credential
 *   configured (`read-only`, detected from the server's "read-only" hint).
 * - 429 ⇒ sign-in lockout; `retryAfterSeconds` comes from the `Retry-After`
 *   header first, then the audit payload (`retry_after`), default 30.
 * - 503 ⇒ privileged action refused because the audit store is unavailable.
 * - anything else ⇒ `client` (4xx) or `server` (5xx), message from the body.
 */

const READ_ONLY_HINT = "read-only";

export type ApiErrorKind =
  | "unauthorized"
  | "forbidden"
  | "read-only"
  | "locked-out"
  | "audit-unavailable"
  | "client"
  | "server";

export class ApiError extends Error {
  readonly status: number;
  readonly kind: ApiErrorKind;
  /** True only for 401 — the session is dead and the caller must re-login. */
  readonly relogin: boolean;
  /** 429 only — how long to wait before retrying (server default 30s). */
  readonly retryAfterSeconds?: number;

  constructor(
    status: number,
    kind: ApiErrorKind,
    message: string,
    opts: { relogin?: boolean; retryAfterSeconds?: number } = {},
  ) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.kind = kind;
    this.relogin = opts.relogin ?? false;
    this.retryAfterSeconds = opts.retryAfterSeconds;
  }
}

/** Best-effort human text out of a FastAPI error body. */
function detailText(payload: unknown): string {
  if (typeof payload === "string") {
    const t = payload.trim();
    return t.length > 0 ? t : "";
  }
  if (payload && typeof payload === "object") {
    const obj = payload as Record<string, unknown>;
    const detail = obj.detail;
    if (typeof detail === "string") return detail;
    if (detail && typeof detail === "object") {
      const msg = (detail as Record<string, unknown>).message;
      if (typeof msg === "string") return msg;
    }
    if (typeof obj.message === "string") return obj.message;
  }
  return "";
}

/** Seconds to wait from a 429: `Retry-After` header first, body as fallback. */
export function retryAfterSeconds(
  response: Response,
  payload: unknown,
  fallback = 30,
): number {
  const header = response.headers.get("retry-after")?.trim();
  if (header) {
    const n = Number.parseFloat(header);
    if (Number.isFinite(n)) return Math.max(Math.trunc(n), 1);
  }
  if (payload && typeof payload === "object") {
    const obj = payload as Record<string, unknown>;
    for (const key of ["retry_after", "retry_after_seconds"]) {
      const value = obj[key];
      if (typeof value === "number" && Number.isFinite(value) && value > 0) {
        return Math.max(Math.trunc(value), 1);
      }
    }
  }
  return fallback;
}

/**
 * Turn any non-2xx response (+ parsed body) into the normalized ApiError.
 * Pure with respect to the network — the only inputs are the response and body.
 */
export function classifyError(response: Response, payload: unknown): ApiError {
  const status = response.status;
  const detail = detailText(payload);

  if (status === 401) {
    return new ApiError(401, "unauthorized", "Your session is no longer valid. Please sign in again.", {
      relogin: true,
    });
  }

  if (status === 403) {
    if (detail.toLowerCase().includes(READ_ONLY_HINT)) {
      return new ApiError(
        403,
        "read-only",
        "This server has no admin credential configured, so it is read-only. " +
          "Set ADMIN_PASSWORD (deprecated) or create a principal, then restart.",
      );
    }
    return new ApiError(403, "forbidden", detail || "You are not allowed to perform this action.");
  }

  if (status === 429) {
    const wait = retryAfterSeconds(response, payload);
    return new ApiError(
      429,
      "locked-out",
      `Too many failed attempts — sign-in is temporarily locked. Try again in ${wait}s.`,
      { retryAfterSeconds: wait },
    );
  }

  if (status === 503) {
    return new ApiError(
      503,
      "audit-unavailable",
      detail ||
        "The audit log is unavailable, so this privileged action was refused. " +
          "Fix the audit store before trying again.",
    );
  }

  if (status >= 500) {
    return new ApiError(status, "server", detail || `The server answered ${status}.`);
  }
  return new ApiError(status, "client", detail || `The server answered ${status}.`);
}

/**
 * Global 401 behavior. `main.tsx` registers a handler that drops session state
 * and routes to `/login`; every relogin-classified ApiError raised through
 * `unwrap` funnels through it, so components never hand-roll redirect logic.
 *
 * A failed login attempt itself answers 401 — the handler decides (by current
 * location) not to treat that as "session died"; callers still get the thrown
 * ApiError to render as a form message.
 */
let unauthorizedHandler: ((error: ApiError) => void) | null = null;

export function setUnauthorizedHandler(handler: (error: ApiError) => void): void {
  unauthorizedHandler = handler;
}

function notifyUnauthorized(error: ApiError): void {
  if (unauthorizedHandler) {
    try {
      unauthorizedHandler(error);
    } catch {
      // The handler must never mask the original error.
    }
  }
}

/**
 * Unwrap an openapi-fetch result: throw the normalized ApiError on any error
 * response, return the typed payload on success. Callers never see `{ data?,
 * error? }` unions.
 */
export async function unwrap<T>(
  result: Promise<{ data?: unknown; error?: unknown; response: Response }>,
): Promise<T> {
  const { data, error, response } = await result;
  if (error !== undefined) {
    const apiError = classifyError(response, error);
    if (apiError.relogin) notifyUnauthorized(apiError);
    throw apiError;
  }
  // openapi-fetch leaves `data` undefined for empty bodies (204/blank 200) —
  // that is a success, not an error. Callers with a typed payload pass T
  // explicitly; several server payloads are `unknown` in the schema (FastAPI
  // returns Dict[str, Any]) and the narrow interfaces in endpoints.ts own them.
  if (data !== undefined) return data as T;
  if (response.ok) return undefined as T;
  throw classifyError(response, undefined);
}
