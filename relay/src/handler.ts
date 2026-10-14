// 中継の中身。Redisと環境を引数で受け取る（テストでは偽物を渡す）。
// api/index.ts（Vercel）と src/dev-server.ts（ローカル）は、これを呼ぶだけ。

import { timingSafeEqual } from "node:crypto";
import type { RedisLike } from "./redis.js";

export interface Deps {
  redis: () => Promise<RedisLike>; // 遅延：トークン検査に通るまでRedisに触らない
  env: { APP_TOKEN?: string; PC_TOKEN?: string };
  now: () => number; // 中継の時計（ミリ秒）
}

export type Role = "app" | "pc";

export function json(status: number, body: unknown, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json; charset=utf-8", "cache-control": "no-store", ...headers },
  });
}

export function error(status: number, code: string, message: string): Response {
  return json(status, { error: code, message });
}

function safeEqual(a: string, b: string): boolean {
  const ab = Buffer.from(a);
  const bb = Buffer.from(b);
  if (ab.length !== bb.length) {
    timingSafeEqual(ab, ab); // 長さの違いで時間が変わりにくくする
    return false;
  }
  return timingSafeEqual(ab, bb);
}

/** トークンから役割を決める。Redisには触らない。 */
export function authenticate(req: Request, env: Deps["env"]): Role | null {
  const h = req.headers.get("authorization") ?? "";
  const m = /^Bearer\s+(\S+)$/.exec(h);
  if (!m) return null;
  const tok = m[1]!;
  // どちらのトークンも未設定・短すぎるなら誰も通さない
  if (env.APP_TOKEN && env.APP_TOKEN.length >= 16 && safeEqual(tok, env.APP_TOKEN)) return "app";
  if (env.PC_TOKEN && env.PC_TOKEN.length >= 16 && safeEqual(tok, env.PC_TOKEN)) return "pc";
  return null;
}

export type RouteFn = (req: Request, deps: Deps, params: Record<string, string>) => Promise<Response>;

export interface Route {
  method: string;
  pattern: RegExp; // 名前付きグループでパラメータを取る
  role: Role;
  fn: RouteFn;
}

export function makeHandler(routes: Route[]) {
  return async function handle(req: Request, deps: Deps): Promise<Response> {
    const role = authenticate(req, deps.env);
    if (!role) return error(401, "unauthorized", "トークンがないか、違います");
    const url = new URL(req.url);
    let pathMatched = false;
    for (const r of routes) {
      const m = r.pattern.exec(url.pathname);
      if (!m) continue;
      pathMatched = true;
      if (r.method !== req.method) continue;
      if (r.role !== role) return error(403, "forbidden", "このトークンでは使えません");
      try {
        return await r.fn(req, deps, (m.groups ?? {}) as Record<string, string>);
      } catch (e) {
        if (e instanceof HttpError) return error(e.status, e.code, e.message);
        console.error(e);
        return error(503, "unavailable", "保存先に届きません");
      }
    }
    return pathMatched ? error(405, "method_not_allowed", "メソッドが違います") : error(404, "not_found", "ありません");
  };
}

export class HttpError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
  ) {
    super(message);
  }
}

/** 本文を上限つきで読む（大きすぎる本文は断る）。 */
export async function readJson(req: Request, maxBytes: number): Promise<unknown> {
  const len = Number(req.headers.get("content-length") ?? "0");
  if (len > maxBytes) throw new HttpError(413, "too_large", `本文が大きすぎます（上限 ${maxBytes} バイト）`);
  const buf = await req.arrayBuffer();
  if (buf.byteLength > maxBytes) throw new HttpError(413, "too_large", `本文が大きすぎます（上限 ${maxBytes} バイト）`);
  try {
    return JSON.parse(new TextDecoder().decode(buf));
  } catch {
    throw new HttpError(400, "bad_json", "JSONが読めません");
  }
}
