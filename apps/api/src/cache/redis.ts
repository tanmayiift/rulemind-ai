import type { CompiledRule } from "@rulemind/rule-engine";
import Redis from "ioredis";

interface SerializedCompiledRule {
  roots: string[];
  nodeMap: Array<[string, unknown]>;
  childMap: Array<[string, string[]]>;
  parentMap: Array<[string, string[]]>;
}

function serialize(compiled: CompiledRule): string {
  const payload: SerializedCompiledRule = {
    roots: compiled.roots,
    nodeMap: [...compiled.nodeMap.entries()],
    childMap: [...compiled.childMap.entries()],
    parentMap: [...compiled.parentMap.entries()]
  };

  return JSON.stringify(payload);
}

function deserialize(raw: string): CompiledRule {
  const payload = JSON.parse(raw) as SerializedCompiledRule;
  return {
    roots: payload.roots,
    nodeMap: new Map(payload.nodeMap as [string, never][]),
    childMap: new Map(payload.childMap),
    parentMap: new Map(payload.parentMap)
  };
}

export class RedisCache {
  private readonly memory = new Map<string, string>();
  private readonly redis?: Redis;

  constructor(redisUrl?: string, private readonly ttlSeconds = 3600) {
    if (redisUrl) {
      this.redis = new Redis(redisUrl, {
        lazyConnect: true,
        maxRetriesPerRequest: 1
      });
      this.redis.connect().catch(() => undefined);
    }
  }

  private compiledKey(ruleId: string, version: number) {
    return `compiled:${ruleId}:v${version}`;
  }

  async getCompiled(ruleId: string, version: number): Promise<CompiledRule | null> {
    const key = this.compiledKey(ruleId, version);

    if (this.redis) {
      try {
        const cached = await this.redis.get(key);
        return cached ? deserialize(cached) : null;
      } catch {
        return this.memory.has(key) ? deserialize(this.memory.get(key) as string) : null;
      }
    }

    return this.memory.has(key) ? deserialize(this.memory.get(key) as string) : null;
  }

  async setCompiled(ruleId: string, version: number, compiled: CompiledRule) {
    const key = this.compiledKey(ruleId, version);
    const value = serialize(compiled);

    this.memory.set(key, value);

    if (this.redis) {
      try {
        await this.redis.set(key, value, "EX", this.ttlSeconds);
      } catch {
        return;
      }
    }
  }

  async invalidateRule(ruleId: string) {
    const prefix = `compiled:${ruleId}:v`;

    [...this.memory.keys()]
      .filter((key) => key.startsWith(prefix))
      .forEach((key) => this.memory.delete(key));

    if (this.redis) {
      try {
        const keys = await this.redis.keys(`${prefix}*`);

        if (keys.length > 0) {
          await this.redis.del(keys);
        }
      } catch {
        return;
      }
    }
  }

  async ping(): Promise<boolean> {
    if (!this.redis) {
      return true;
    }

    try {
      const result = await this.redis.ping();
      return result === "PONG";
    } catch {
      return false;
    }
  }
}
