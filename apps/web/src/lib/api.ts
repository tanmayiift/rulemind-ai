"use client";

export async function apiJson<T>(
  apiBaseUrl: string,
  path: string,
  init: RequestInit = {},
  apiKey = ""
): Promise<T> {
  const response = await fetch(`${apiBaseUrl.replace(/\/$/, "")}${path}`, {
    ...init,
    headers: {
      ...(init.body ? { "content-type": "application/json" } : {}),
      ...(apiKey ? { "x-api-key": apiKey } : {}),
      ...(init.headers ?? {}),
    },
    cache: "no-store",
  });

  if (!response.ok) {
    throw new Error(await response.text());
  }

  return response.json() as Promise<T>;
}

export async function apiText(
  apiBaseUrl: string,
  path: string,
  init: RequestInit = {},
  apiKey = ""
): Promise<{ text: string; response: Response }> {
  const response = await fetch(`${apiBaseUrl.replace(/\/$/, "")}${path}`, {
    ...init,
    headers: {
      ...(apiKey ? { "x-api-key": apiKey } : {}),
      ...(init.headers ?? {}),
    },
    cache: "no-store",
  });

  if (!response.ok) {
    throw new Error(await response.text());
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
