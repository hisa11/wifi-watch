import { FakeRedis } from "../src/fake-redis.js";
import { handle } from "../src/routes.js";

export const APP = "app-token-0123456789abcdef";
export const PC = "pc-token-0123456789abcdef!";

export function env(start = 1_791_936_000_000) {
  let now = start;
  const redis = new FakeRedis(() => now);
  let touched = 0;
  const deps = {
    redis: async () => {
      touched++;
      return redis;
    },
    env: { APP_TOKEN: APP, PC_TOKEN: PC },
    now: () => now,
  };
  async function call(method: string, path: string, token: string | null, body?: unknown) {
    const headers: Record<string, string> = {};
    if (token) headers.authorization = `Bearer ${token}`;
    let payload: string | undefined;
    if (body !== undefined && method !== "GET") {
      payload = typeof body === "string" ? body : JSON.stringify(body);
      headers["content-type"] = "application/json";
    }
    const res = await handle(new Request(`https://relay.test${path}`, { method, headers, body: payload }), deps);
    const text = await res.text();
    return { status: res.status, body: text ? JSON.parse(text) : null };
  }
  return {
    redis,
    touched: () => touched,
    advance: (ms: number) => {
      now += ms;
    },
    now: () => now,
    app: (method: string, path: string, body?: unknown) => call(method, path, APP, body),
    pc: (method: string, path: string, body?: unknown) => call(method, path, PC, body),
    raw: call,
    /** 1回の要求で使ったコマンド数を数える */
    async count<T>(fn: () => Promise<T>): Promise<[T, number]> {
      const before = redis.usage.commands;
      const r = await fn();
      return [r, redis.usage.commands - before];
    },
  };
}
