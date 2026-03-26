import { createHash, randomBytes } from "node:crypto";

export function generateApiKey(environment: string): string {
  const suffix = randomBytes(18).toString("hex");
  return `re_${environment}_${suffix}`;
}

export function hashApiKey(rawKey: string, salt: string): string {
  return createHash("sha256").update(`${salt}:${rawKey}`).digest("hex");
}
