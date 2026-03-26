import { randomUUID } from "node:crypto";
import type { AppConfig } from "../config";

export async function initTracing(_config: AppConfig) {
  return;
}

export function resolveTraceId(candidate?: string) {
  const value = candidate?.trim();
  return value ? value : `tr_${randomUUID()}`;
}
