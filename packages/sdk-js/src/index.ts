export interface RuleMindServerClientConfig {
  baseUrl: string;
  apiKey?: string;
  sdkVersion?: string;
  timeoutMs?: number;
  retries?: number;
}

export interface SdkDecideRequest {
  policyId: string;
  payload: Record<string, unknown>;
  userId?: string;
  requestId?: string;
  sdkVersion?: string;
}

export interface SdkEventRecord {
  type: string;
  timestamp?: string;
  data?: Record<string, unknown>;
}

export interface SdkDecideResponse {
  outcome: string;
  score?: number | null;
  variables: Record<string, unknown>;
  ruleResults: Array<Record<string, unknown>>;
  experiment?: { id?: string | null; variant?: string | null } | null;
  latencyMs: number;
  requestId?: string | null;
  serverOnlyStepsSkipped: string[];
}

export interface SdkEventsResponse {
  received: number;
  processed: number;
}

export interface SdkBundleResponse {
  bundleVersion: number;
  encryptedBundle: string;
  encryptedKey?: string | null;
  signature: string;
  checksum: string;
  compiledAt: string;
  expiresAt: string;
}

type RequestOptions = Omit<RequestInit, "body"> & { body?: unknown };

export class RuleMindServerClient {
  private readonly baseUrl: string;
  private readonly apiKey?: string;
  private readonly sdkVersion: string;
  private readonly timeoutMs: number;
  private readonly retries: number;

  constructor(config: RuleMindServerClientConfig) {
    this.baseUrl = config.baseUrl.replace(/\/$/, "");
    this.apiKey = config.apiKey;
    this.sdkVersion = config.sdkVersion ?? "4.0.0";
    this.timeoutMs = config.timeoutMs ?? 5000;
    this.retries = config.retries ?? 0;
  }

  private async request<T>(path: string, init: RequestOptions = {}): Promise<T> {
    for (let attempt = 0; attempt <= this.retries; attempt += 1) {
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), this.timeoutMs);
      try {
        const response = await fetch(`${this.baseUrl}${path}`, {
          ...init,
          signal: controller.signal,
          headers: {
            "content-type": "application/json",
            ...(this.apiKey ? { "x-api-key": this.apiKey } : {}),
            ...(init.headers ?? {})
          },
          body: init.body === undefined ? undefined : JSON.stringify(init.body)
        });
        if (response.status === 304) {
          return { notModified: true } as T;
        }
        if (!response.ok) {
          const message = await response.text();
          if (response.status >= 500 && attempt < this.retries) {
            continue;
          }
          throw new Error(message || `RuleMind request failed with ${response.status}`);
        }
        return (await response.json()) as T;
      } catch (error) {
        if (controller.signal.aborted) {
          throw new Error(`RuleMind request timed out after ${this.timeoutMs}ms`);
        }
        if (attempt >= this.retries) {
          throw error instanceof Error ? error : new Error(String(error));
        }
      } finally {
        clearTimeout(timeout);
      }
    }
    throw new Error("RuleMind request failed after retries.");
  }

  async decide(request: SdkDecideRequest): Promise<SdkDecideResponse> {
    return this.request<SdkDecideResponse>("/sdk/v1/decide", { method: "POST", body: request });
  }

  async sendEvents(events: SdkEventRecord[]): Promise<SdkEventsResponse> {
    return this.request<SdkEventsResponse>("/sdk/v1/events", { method: "POST", body: { events } });
  }

  async getBundle(options: { bundleVersion?: number; clientPublicKey?: string } = {}): Promise<SdkBundleResponse | null> {
    const headers: Record<string, string> = { "x-sdk-version": this.sdkVersion };
    if (options.bundleVersion !== undefined) {
      headers["x-bundle-version"] = String(options.bundleVersion);
    }
    if (options.clientPublicKey) {
      headers["x-client-public-key"] = options.clientPublicKey;
    }
    const response = await fetch(`${this.baseUrl}/sdk/v1/bundle`, {
      method: "GET",
      headers: {
        ...(this.apiKey ? { "x-api-key": this.apiKey } : {}),
        ...headers
      }
    });
    if (response.status === 304) {
      return null;
    }
    if (!response.ok) {
      throw new Error(await response.text() || `RuleMind request failed with ${response.status}`);
    }
    return (await response.json()) as SdkBundleResponse;
  }
}

export { RuleMindServerClient as RuleMindClient, RuleMindServerClient as RuleEngineClient };
export type RuleMindClientConfig = RuleMindServerClientConfig;
