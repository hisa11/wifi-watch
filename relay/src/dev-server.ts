// ローカルの中継（開発とテスト用）。本番と同じ handle と、偽のRedisを使う。
// vercel dev もVercelへのログインも要らない。127.0.0.1 にだけ bind する。
//   APP_TOKEN=... PC_TOKEN=... node dist/src/dev-server.js [ポート]
import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { createServer, type IncomingMessage, type ServerResponse } from "node:http";
import { FakeRedis } from "./fake-redis.js";
import { handle } from "./routes.js";

export function startDevServer(opts: {
  port: number;
  appToken: string;
  pcToken: string;
  redis?: FakeRedis;
  now?: () => number;
}) {
  const redis = opts.redis ?? new FakeRedis(opts.now);
  const now = opts.now ?? (() => Date.now());
  const server = createServer(async (req: IncomingMessage, res: ServerResponse) => {
    const chunks: Buffer[] = [];
    for await (const c of req) chunks.push(c as Buffer);
    const body = Buffer.concat(chunks);
    const headers = new Headers();
    for (const [k, v] of Object.entries(req.headers)) if (typeof v === "string") headers.set(k, v);
    const request = new Request(`http://127.0.0.1:${opts.port}${req.url ?? "/"}`, {
      method: req.method,
      headers,
      body: req.method === "GET" || req.method === "HEAD" ? undefined : body,
    });
    const response = await handle(request, {
      redis: async () => redis,
      env: { APP_TOKEN: opts.appToken, PC_TOKEN: opts.pcToken },
      now,
    });
    res.statusCode = response.status;
    response.headers.forEach((v, k) => res.setHeader(k, v));
    res.end(Buffer.from(await response.arrayBuffer()));
  });
  return new Promise<{ server: typeof server; redis: FakeRedis; port: number }>((resolve) => {
    server.listen(opts.port, "127.0.0.1", () => {
      const addr = server.address();
      resolve({ server, redis, port: typeof addr === "object" && addr ? addr.port : opts.port });
    });
  });
}

const isMain = process.argv[1] && import.meta.url === new URL(`file://${process.argv[1]}`).href;
if (isMain) {
  const port = Number(process.argv[2] ?? process.env.PORT ?? 8787);
  const appToken = process.env.APP_TOKEN ?? "";
  const pcToken = process.env.PC_TOKEN ?? "";
  if (appToken.length < 16 || pcToken.length < 16) {
    console.error("APP_TOKEN と PC_TOKEN を16文字以上で指定してください");
    process.exit(2);
  }
  // REDIS_STATE=ファイル を指定すると、偽のRedisの中身をそこに残す（止めて戻しても消えない＝Upstashと同じ）
  const statePath = process.env.REDIS_STATE;
  const redis = new FakeRedis();
  if (statePath && existsSync(statePath)) redis.load(readFileSync(statePath, "utf-8"));
  startDevServer({ port, appToken, pcToken, redis }).then(({ port: p }) => {
    console.log(`ローカルの中継: http://127.0.0.1:${p}${statePath ? `（Redisの中身：${statePath}）` : ""}`);
    if (statePath) {
      let last = "";
      const save = () => {
        const text = redis.dump();
        if (text !== last) { writeFileSync(statePath, text, { mode: 0o600 }); last = text; }
      };
      setInterval(save, 500).unref();
      for (const sig of ["SIGINT", "SIGTERM"] as const) process.on(sig, () => { save(); process.exit(0); });
    }
    setInterval(() => {
      if (redis.usage.commands) console.log(`redis: ${redis.usage.commands} commands, ${redis.usage.bytes} bytes`);
    }, 60_000).unref();
  });
}
