"use client";

// A human-login session bearer token, when one is active. It takes precedence over
// the (dev) API key: a logged-in member acts as themselves with their own role, so
// we send Authorization: Bearer and omit x-api-key. Kept in sync with the store.
let _bearerToken = "";

export function setBearerToken(token: string): void {
  _bearerToken = token || "";
}

export function getBearerToken(): string {
  return _bearerToken;
}

function authHeaders(apiKey: string): Record<string, string> {
  if (_bearerToken) return { Authorization: `Bearer ${_bearerToken}` };
  return apiKey ? { "x-api-key": apiKey } : {};
}

export async function apiJson<T>(
  apiBaseUrl: string,
  path: string,
  init: RequestInit = {},
  apiKey = ""
): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${apiBaseUrl.replace(/\/$/, "")}${path}`, {
      ...init,
      headers: {
        ...(init.body ? { "content-type": "application/json" } : {}),
        ...authHeaders(apiKey),
        ...(init.headers ?? {}),
      },
      cache: "no-store",
    });
  } catch {
    throw new Error(`Unable to reach the API server at ${apiBaseUrl}. Check that the backend is running and the URL is correct.`);
  }

  if (!response.ok) {
    const body = await response.text().catch(() => "");
    throw new Error(body || `Request failed with status ${response.status}`);
  }

  return response.json() as Promise<T>;
}

export async function apiText(
  apiBaseUrl: string,
  path: string,
  init: RequestInit = {},
  apiKey = ""
): Promise<{ text: string; response: Response }> {
  let response: Response;
  try {
    response = await fetch(`${apiBaseUrl.replace(/\/$/, "")}${path}`, {
      ...init,
      headers: {
        ...authHeaders(apiKey),
        ...(init.headers ?? {}),
      },
      cache: "no-store",
    });
  } catch {
    throw new Error(`Unable to reach the API server at ${apiBaseUrl}. Check that the backend is running and the URL is correct.`);
  }

  if (!response.ok) {
    const body = await response.text().catch(() => "");
    throw new Error(body || `Request failed with status ${response.status}`);
  }

  return { text: await response.text(), response };
}

export async function rulemindRequest<T>(
  apiBaseUrl: string,
  apiKey: string,
  path: string,
  init: RequestInit = {}
): Promise<T> {
  return apiJson<T>(apiBaseUrl, path, init, apiKey);
}

export interface StreamedDecision {
  id?: string;
  policy_id?: string;
  outcome?: string;
  source?: string;
  latency_ms?: number;
  experiment_variant?: string;
  created_at?: string;
}

/**
 * Subscribe to the live decision feed (GET /api/v1/decisions/stream) via a fetch stream.
 * EventSource can't send an auth header, so we read the text/event-stream body ourselves,
 * parse `data:` frames, and invoke `onDecision` for each. Returns a stop() that aborts the
 * connection. Reconnection is left to the caller (toggle off/on) — the server also bounds the
 * connection lifetime so a long-lived stream is refreshed rather than held forever.
 */
export function streamDecisions(
  apiBaseUrl: string,
  apiKey: string,
  onDecision: (decision: StreamedDecision) => void,
  onError?: (error: unknown) => void
): () => void {
  const controller = new AbortController();
  (async () => {
    try {
      const response = await fetch(`${apiBaseUrl.replace(/\/$/, "")}/api/v1/decisions/stream`, {
        headers: { accept: "text/event-stream", ...authHeaders(apiKey) },
        cache: "no-store",
        signal: controller.signal,
      });
      if (!response.ok || !response.body) {
        throw new Error(`Live feed failed with status ${response.status}`);
      }
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        // SSE frames are separated by a blank line.
        let split: number;
        while ((split = buffer.indexOf("\n\n")) !== -1) {
          const frame = buffer.slice(0, split);
          buffer = buffer.slice(split + 2);
          const dataLine = frame
            .split("\n")
            .find((line) => line.startsWith("data:"));
          if (!dataLine) continue;
          try {
            onDecision(JSON.parse(dataLine.slice(5).trim()) as StreamedDecision);
          } catch {
            /* ignore malformed frame */
          }
        }
      }
    } catch (error) {
      if (!controller.signal.aborted) onError?.(error);
    }
  })();
  return () => controller.abort();
}
