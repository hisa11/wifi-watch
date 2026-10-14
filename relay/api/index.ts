// Vercel Functions の入口（薄い層）。/api/* はすべてここに来る（vercel.json の rewrites）。
import { handle } from "../src/routes.js";
import { upstashFromEnv, type RedisLike } from "../src/redis.js";

let client: Promise<RedisLike> | null = null;

export default {
  async fetch(request: Request): Promise<Response> {
    return handle(request, {
      redis: () => (client ??= upstashFromEnv(process.env)),
      env: { APP_TOKEN: process.env.APP_TOKEN, PC_TOKEN: process.env.PC_TOKEN },
      now: () => Date.now(),
    });
  },
};
