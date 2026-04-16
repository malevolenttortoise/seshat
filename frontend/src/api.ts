// Thin wrapper over fetch() that prefixes /api, handles JSON, and
// throws an Error on any non-2xx response. The thrown Error carries
// `.status` so callers can branch on specific failures.
//
// Any 401 response dispatches the `seshat:auth-required` window
// event. App.tsx listens and drops the user back to the login screen
// without each call site having to handle auth failures individually.

export class ApiError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.status = status;
  }
}

async function check(r: Response): Promise<unknown> {
  if (r.ok) {
    if (r.status === 204) return null;
    return r.json();
  }
  let detail = String(r.status);
  try {
    const j = (await r.json()) as { detail?: string | object };
    if (j && j.detail) {
      detail =
        typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail);
    }
  } catch {
    /* ignore */
  }
  if (r.status === 401) {
    window.dispatchEvent(new CustomEvent("seshat:auth-required"));
  }
  throw new ApiError(detail, r.status);
}

export const api = {
  get: async <T>(url: string, signal?: AbortSignal): Promise<T> => {
    const r = await fetch(`/api${url}`, signal ? { signal } : undefined);
    return (await check(r)) as T;
  },
  post: async <T>(
    url: string,
    body?: unknown,
    signal?: AbortSignal,
  ): Promise<T> => {
    const init: RequestInit = { method: "POST" };
    if (body !== undefined) {
      init.headers = { "Content-Type": "application/json" };
      init.body = JSON.stringify(body);
    }
    if (signal) init.signal = signal;
    const r = await fetch(`/api${url}`, init);
    return (await check(r)) as T;
  },
  put: async <T>(
    url: string,
    body?: unknown,
    signal?: AbortSignal,
  ): Promise<T> => {
    const init: RequestInit = { method: "PUT" };
    if (body !== undefined) {
      init.headers = { "Content-Type": "application/json" };
      init.body = JSON.stringify(body);
    }
    if (signal) init.signal = signal;
    const r = await fetch(`/api${url}`, init);
    return (await check(r)) as T;
  },
  patch: async <T>(
    url: string,
    body?: unknown,
    signal?: AbortSignal,
  ): Promise<T> => {
    const init: RequestInit = { method: "PATCH" };
    if (body !== undefined) {
      init.headers = { "Content-Type": "application/json" };
      init.body = JSON.stringify(body);
    }
    if (signal) init.signal = signal;
    const r = await fetch(`/api${url}`, init);
    return (await check(r)) as T;
  },
  del: async <T>(url: string, signal?: AbortSignal): Promise<T> => {
    const init: RequestInit = { method: "DELETE" };
    if (signal) init.signal = signal;
    const r = await fetch(`/api${url}`, init);
    return (await check(r)) as T;
  },
  isAbort: (e: unknown): boolean => {
    return (
      e instanceof DOMException && (e.name === "AbortError" || e.code === 20)
    );
  },
};
