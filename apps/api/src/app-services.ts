import type { RedisCache } from "./cache/redis";
import type { AppConfig } from "./config";
import type { PlatformStore } from "./db/adapter";
import type { RuleMindMetrics } from "./observability/metrics";
import type { Logger } from "./observability/logger";

export interface AppServices {
  config: AppConfig;
  store: PlatformStore;
  cache: RedisCache;
  metrics: RuleMindMetrics;
  logger: Logger;
}
