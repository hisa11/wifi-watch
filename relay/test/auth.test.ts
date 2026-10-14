import { describe, expect, it } from "vitest";
import { FakeRedis } from "../src/fake-redis.js";
import { handle } from "../src/routes.js";

const APP = "a".repeat(32);
const PC = "p".repeat(32);

function deps(redis = new FakeRedis()) {
  let touched = 0;
  return {
    redis,
    touched: () => touched,
    d: {
      redis: async () => {
        touched++;
        return redis;
      },
      env: { APP_TOKEN: APP, PC_TOKEN: PC },
      now: () => 1_791_936_000_000,
    },
  };
}

function req(path: string, token?: string, method = "POST") {
  const headers: Record<string, string> = {};
  if (token) headers.authorization = `Bearer ${token}`;
  return new Request(`http://relay.test${path}`, { method, headers });
}

describe("token", () => {
  it("rejects missing and wrong tokens without touching redis", async () => {
    const x = deps();
    for (const t of [undefined, "wrong", APP + "x", ""]) {
      const r = await handle(req("/api/app/poll", t), x.d);
      expect(r.status).toBe(401);
    }
    expect(x.touched()).toBe(0);
    expect(x.redis.usage.commands).toBe(0);
  });

  it("refuses everything when tokens are not configured", async () => {
    const x = deps();
    x.d.env = { APP_TOKEN: "", PC_TOKEN: "" };
    const r = await handle(req("/api/app/poll", ""), x.d);
    expect(r.status).toBe(401);
  });
});
