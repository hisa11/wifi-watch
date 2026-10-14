// 中継が使うRedisの操作だけを並べたインタフェース。
// 本番は Upstash（REST）、テストとローカルの中継はメモリ上の偽物（src/fake-redis.ts）を渡す。
// 値はすべて文字列で扱う（JSONの直列化は呼ぶ側で行う）。1メソッド＝Redisの1コマンド。

export interface SetOptions {
  ex?: number; // 秒
  nx?: boolean;
}

export interface RedisLike {
  get(key: string): Promise<string | null>;
  mget(keys: string[]): Promise<(string | null)[]>;
  set(key: string, value: string, opts?: SetOptions): Promise<boolean>;
  del(keys: string[]): Promise<number>;
  incr(key: string): Promise<number>;
  incrby(key: string, by: number): Promise<number>;
  hget(key: string, field: string): Promise<string | null>;
  hmget(key: string, fields: string[]): Promise<(string | null)[]>;
  hgetall(key: string): Promise<Record<string, string>>;
  hset(key: string, values: Record<string, string>): Promise<number>;
  hsetnx(key: string, field: string, value: string): Promise<boolean>;
  hdel(key: string, fields: string[]): Promise<number>;
  zadd(key: string, score: number, member: string): Promise<number>;
  /** スコアが min 以上 max 以下を昇順で。limit 件まで。 */
  zrangeByScore(key: string, min: number, max: number, limit: number): Promise<string[]>;
  zrem(key: string, members: string[]): Promise<number>;
}

/** @upstash/redis を RedisLike に合わせる薄い層。 */
export async function upstashFromEnv(env: Record<string, string | undefined>): Promise<RedisLike> {
  // VercelのMarketplaceでつなぐと入る名前は2通りありうる（docs/relay.md）
  const url = env.UPSTASH_REDIS_REST_URL ?? env.KV_REST_API_URL;
  const token = env.UPSTASH_REDIS_REST_TOKEN ?? env.KV_REST_API_TOKEN;
  if (!url || !token) throw new Error("Upstashの接続情報がありません");
  const { Redis } = await import("@upstash/redis");
  const r = new Redis({ url, token, automaticDeserialization: false, enableTelemetry: false });
  const str = (v: unknown): string | null => (v === null || v === undefined ? null : String(v));
  return {
    get: async (k) => str(await r.get<string>(k)),
    mget: async (ks) => (ks.length ? (await r.mget<(string | null)[]>(...ks)).map(str) : []),
    set: async (k, v, o) => {
      const opts: Record<string, unknown> = {};
      if (o?.ex) opts.ex = o.ex;
      if (o?.nx) opts.nx = true;
      const res = await r.set(k, v, opts as never);
      return res === "OK";
    },
    del: async (ks) => (ks.length ? await r.del(...ks) : 0),
    incr: (k) => r.incr(k),
    incrby: (k, n) => r.incrby(k, n),
    hget: async (k, f) => str(await r.hget<string>(k, f)),
    hmget: async (k, fs) => {
      if (!fs.length) return [];
      const res = (await r.hmget<Record<string, string | null>>(k, ...fs)) ?? {};
      return fs.map((f) => str(res[f]));
    },
    hgetall: async (k) => {
      const res = (await r.hgetall<Record<string, unknown>>(k)) ?? {};
      const out: Record<string, string> = {};
      for (const [f, v] of Object.entries(res)) out[f] = String(v);
      return out;
    },
    hset: (k, vals) => r.hset(k, vals),
    hsetnx: async (k, f, v) => (await r.hsetnx(k, f, v)) === 1,
    hdel: async (k, fs) => (fs.length ? await r.hdel(k, ...fs) : 0),
    zadd: async (k, score, member) => (await r.zadd(k, { score, member })) ?? 0,
    zrangeByScore: async (k, min, max, limit) =>
      (await r.zrange<string[]>(k, min, max, { byScore: true, offset: 0, count: limit })).map(String),
    zrem: async (k, ms) => (ms.length ? await r.zrem(k, ...ms) : 0),
  };
}
