import pino from "pino";
import type { AppConfig } from "../config";

export type Logger = ReturnType<typeof createLogger>;

export function createLogger(config: AppConfig) {
  return pino({
    level: config.logLevel,
    base: undefined,
    timestamp: pino.stdTimeFunctions.isoTime
  });
}
