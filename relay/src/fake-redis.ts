// メモリ上の偽のRedis。コマンド数と転送量（UpstashのRESTに近い数え方）を数える。
// テストとローカルの中継（src/dev-server.ts）で使う。

import type { RedisLike, SetOptions } from "./redis.js";

type Entry =
  | { t: "s"; v: string; exp?: number }
  | { t: "h"; v: Map<string, string>; exp?: number }
  | { t: "z"; v: Map<string, number>; exp?: number };

export interface Usage {
  commands: number;
  bytes: number;
  byCommand: Record<string, number>;
}

export class FakeRedis implements RedisLike {
  private data = new Map<string, Entry>();
  usage: Usage = { commands: 0, bytes: 0, byCommand: {} };
  /** 失敗を注入する（trueの間はすべてのコマンドが例外） */
  down = false;

  constructor(private now: () => number = () => Date.now()) {}

  resetUsage(): void {
    this.usage = { commands: 0, bytes: 0, byCommand: {} };
  }

  /** Redisが空に戻ったことにする（版数の比較が「違うか」で行われることのテスト用） */
  flushAll(): void {
    this.data.clear();
  }

  private count(cmd: unknown[], result: unknown): void {
    if (this.down) throw new Error("redis down");
    const name = String(cmd[0]);
    this.usage.commands += 1;
    this.usage.byCommand[name] = (this.usage.byCommand[name] ?? 0) + 1;
    // 要求 ["CMD", ...args] と応答 {"result": ...} のJSONの長さ
    this.usage.bytes += Buffer.byteLength(JSON.stringify(cmd)) + Buffer.byteLength(JSON.stringify({ result }));
  }

  private live(key: string): Entry | undefined {
    const e = this.data.get(key);
    if (e && e.exp !== undefined && e.exp <= this.now()) {
      this.data.delete(key);
      return undefined;
    }
    return e;
  }

  private str(key: string): string | null {
    const e = this.live(key);
    if (!e) return null;
    if (e.t !== "s") throw new Error("WRONGTYPE");
    return e.v;
  }

  private hash(key: string, create: boolean): Map<string, string> | undefined {
    const e = this.live(key);
    if (!e) {
      if (!create) return undefined;
      const m = new Map<string, string>();
      this.data.set(key, { t: "h", v: m });
      return m;
    }
    if (e.t !== "h") throw new Error("WRONGTYPE");
    return e.v;
  }

  private zset(key: string, create: boolean): Map<string, number> | undefined {
    const e = this.live(key);
    if (!e) {
      if (!create) return undefined;
      const m = new Map<string, number>();
      this.data.set(key, { t: "z", v: m });
      return m;
    }
    if (e.t !== "z") throw new Error("WRONGTYPE");
    return e.v;
  }

  async get(key: string) {
    const r = this.str(key);
    this.count(["GET", key], r);
    return r;
  }

  async mget(keys: string[]) {
    const r = keys.map((k) => this.str(k));
    this.count(["MGET", ...keys], r);
    return r;
  }

  async set(key: string, value: string, opts?: SetOptions) {
    let ok = true;
    if (opts?.nx && this.live(key)) ok = false;
    if (ok) this.data.set(key, { t: "s", v: value, exp: opts?.ex ? this.now() + opts.ex * 1000 : undefined });
    const cmd: unknown[] = ["SET", key, value];
    if (opts?.ex) cmd.push("EX", opts.ex);
    if (opts?.nx) cmd.push("NX");
    this.count(cmd, ok ? "OK" : null);
    return ok;
  }

  async del(keys: string[]) {
    let n = 0;
    for (const k of keys) if (this.live(k) && this.data.delete(k)) n++;
    this.count(["DEL", ...keys], n);
    return n;
  }

  async incr(key: string) {
    return this.incrbyNamed("INCR", key, 1);
  }

  async incrby(key: string, by: number) {
    return this.incrbyNamed("INCRBY", key, by);
  }

  private incrbyNamed(name: string, key: string, by: number) {
    const cur = Number(this.str(key) ?? "0");
    const v = cur + by;
    const e = this.live(key);
    this.data.set(key, { t: "s", v: String(v), exp: e?.exp });
    this.count(name === "INCR" ? [name, key] : [name, key, by], v);
    return v;
  }

  async hget(key: string, field: string) {
    const r = this.hash(key, false)?.get(field) ?? null;
    this.count(["HGET", key, field], r);
    return r;
  }

  async hmget(key: string, fields: string[]) {
    const h = this.hash(key, false);
    const r = fields.map((f) => h?.get(f) ?? null);
    this.count(["HMGET", key, ...fields], r);
    return r;
  }

  async hgetall(key: string) {
    const h = this.hash(key, false);
    const out: Record<string, string> = {};
    if (h) for (const [f, v] of h) out[f] = v;
    this.count(["HGETALL", key], Object.entries(out).flat());
    return out;
  }

  async hset(key: string, values: Record<string, string>) {
    const h = this.hash(key, true)!;
    let added = 0;
    for (const [f, v] of Object.entries(values)) {
      if (!h.has(f)) added++;
      h.set(f, v);
    }
    this.count(["HSET", key, ...Object.entries(values).flat()], added);
    return added;
  }

  async hsetnx(key: string, field: string, value: string) {
    const h = this.hash(key, true)!;
    const ok = !h.has(field);
    if (ok) h.set(field, value);
    this.count(["HSETNX", key, field, value], ok ? 1 : 0);
    return ok;
  }

  async hdel(key: string, fields: string[]) {
    const h = this.hash(key, false);
    let n = 0;
    if (h) for (const f of fields) if (h.delete(f)) n++;
    if (h && h.size === 0) this.data.delete(key);
    this.count(["HDEL", key, ...fields], n);
    return n;
  }

  async zadd(key: string, score: number, member: string) {
    const z = this.zset(key, true)!;
    const added = z.has(member) ? 0 : 1;
    z.set(member, score);
    this.count(["ZADD", key, score, member], added);
    return added;
  }

  async zrangeByScore(key: string, min: number, max: number, limit: number) {
    const z = this.zset(key, false);
    const r = z
      ? [...z.entries()]
          .filter(([, s]) => s >= min && s <= max)
          .sort((a, b) => a[1] - b[1] || (a[0] < b[0] ? -1 : 1))
          .slice(0, limit)
          .map(([m]) => m)
      : [];
    this.count(["ZRANGE", key, min, max, "BYSCORE", "LIMIT", 0, limit], r);
    return r;
  }

  async zrem(key: string, members: string[]) {
    const z = this.zset(key, false);
    let n = 0;
    if (z) for (const m of members) if (z.delete(m)) n++;
    this.count(["ZREM", key, ...members], n);
    return n;
  }
}
