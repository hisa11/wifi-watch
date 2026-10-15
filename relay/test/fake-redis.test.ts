import { describe, expect, it } from "vitest";
import { FakeRedis } from "../src/fake-redis.js";

describe("偽のRedisの中身を残す（ローカルの中継を止めて戻す確認用）", () => {
  it("dump と load で文字列・ハッシュ・ソート済み集合と期限が戻る", async () => {
    let t = 1_000;
    const a = new FakeRedis(() => t);
    await a.set("ww:ver", "5");
    await a.set("ww:fg", "1", { ex: 5 });
    await a.hset("ww:inbox", { "cmd:1": '{"x":1}' });
    await a.zadd("ww:recupd", 3, "rec-1");
    const b = new FakeRedis(() => t);
    b.load(a.dump());
    expect(await b.get("ww:ver")).toBe("5");
    expect(await b.hgetall("ww:inbox")).toEqual({ "cmd:1": '{"x":1}' });
    expect(await b.zrangeByScore("ww:recupd", 0, 10, 10)).toEqual(["rec-1"]);
    t += 6_000;
    expect(await b.get("ww:fg")).toBeNull(); // 期限も引き継ぐ
  });
});
