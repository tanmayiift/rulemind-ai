"use client";

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
        ...(apiKey ? { "x-api-key": apiKey } : {}),
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
        ...(apiKey ? { "x-api-key": apiKey } : {}),
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
