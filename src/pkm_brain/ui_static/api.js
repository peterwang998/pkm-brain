/**
 * Brain UI fallback API client. Contract: docs/specs/app-and-operations.md.
 */
export class ApiError extends Error {
  constructor(status, message, payload = null) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.payload = payload;
  }
}

const inflight = new Map();

export async function api(path, options = {}) {
  const method = (options.method || "GET").toUpperCase();
  const token = localStorage.getItem("brain_token") || "";
  const headers = new Headers(options.headers || {});
  if (token) headers.set("Authorization", `Bearer ${token}`);
  let body = options.body;
  if (body !== undefined && body !== null && typeof body !== "string") {
    headers.set("Content-Type", "application/json");
    body = JSON.stringify(body);
  }
  const key = method === "GET" ? `${method} ${path}` : "";
  if (key && inflight.has(key)) return inflight.get(key);
  const request = fetch(path, {...options, method, headers, body})
    .then(async response => {
      const text = await response.text();
      let payload = null;
      if (text) {
        try {
          payload = JSON.parse(text);
        } catch (_error) {
          payload = {error: text};
        }
      }
      if (!response.ok) {
        const message = payload?.error || `HTTP ${response.status}`;
        if (response.status === 401) {
          window.dispatchEvent(new CustomEvent("brain:auth-required"));
        }
        throw new ApiError(response.status, message, payload);
      }
      return payload;
    })
    .finally(() => {
      if (key) inflight.delete(key);
    });
  if (key) inflight.set(key, request);
  return request;
}
