import { describe, expect, it } from "vitest";
import { APP, PC, env } from "./helpers.js";

const scan = (rid: string, params: Record<string, unknown> = { bands: ["6"] }) => ({ request_id: rid, type: "scan", params });

describe("トークン（Redisに触る前に断る）", () => {
  it("トークンなし・違うトークン・役割違いは断り、Redisのコマンドを1つも使わない", async () => {
    const e = env();
    const cases: [string, string, string | null][] = [
      ["POST", "/api/app/poll", null],
      ["POST", "/api/app/poll", "wrong-token-wrong-token"],
      ["POST", "/api/pc/sync", APP], // アプリ用のトークンでPC側を使う
      ["PUT", "/api/pc/records/x", APP],
      ["POST", "/api/app/commands", PC], // PC用のトークンでアプリ側
      ["GET", "/api/app/records", null],
    ];
    for (const [m, p, t] of cases) {
      const r = await e.raw(m, p, t, {});
      expect([401, 403]).toContain(r.status);
    }
    expect(e.touched()).toBe(0);
    expect(e.redis.usage.commands).toBe(0);
  });

  it("知らない口は404、メソッド違いは405", async () => {
    const e = env();
    expect((await e.app("GET", "/api/app/nope")).status).toBe(404);
    expect((await e.app("DELETE", "/api/app/poll")).status).toBe(405);
  });
});

describe("コマンド数の上限（無料枠を守る）", () => {
  it("PCの待機中は1、アプリ前面中は4以下、アプリの問い合わせは2（開いた直後3）", async () => {
    const e = env();
    let [r, n] = await e.count(() => e.pc("POST", "/api/pc/sync", { version: null }));
    expect(n).toBeLessThanOrEqual(4);
    const v = r.body.version;
    [r, n] = await e.count(() => e.pc("POST", "/api/pc/sync", { version: v }));
    expect(n).toBe(1); // 待機中
    expect(r.body.changed).toBe(false);
    expect(r.body.fg).toBe(false);
    [r, n] = await e.count(() => e.app("POST", "/api/app/poll", { opened: true }));
    expect(n).toBe(3);
    [r, n] = await e.count(() => e.app("POST", "/api/app/poll", {}));
    expect(n).toBe(2);
    [r, n] = await e.count(() => e.pc("POST", "/api/pc/sync", { version: v, state: { mode: "LIVE" } }));
    expect(n).toBeLessThanOrEqual(4);
    expect(r.body.fg).toBe(true);
    expect(r.body.changed).toBe(true);
    const v2 = r.body.version;
    [r, n] = await e.count(() => e.pc("POST", "/api/pc/sync", { version: v2, state: { mode: "LIVE" } }));
    expect(n).toBe(2); // 前面中で変化なし：読む1＋状態の写し1
  });

  it("置く操作は5以内", async () => {
    const e = env();
    let [, n] = await e.count(() => e.app("POST", "/api/app/commands", scan("r1")));
    expect(n).toBeLessThanOrEqual(5);
    [, n] = await e.count(() => e.app("POST", "/api/app/commands", { request_id: "b1", type: "bands", params: { excluded_bands: ["2.4"], excluded_freqs: [] } }));
    expect(n).toBeLessThanOrEqual(5);
    [, n] = await e.count(() => e.app("PATCH", "/api/app/records/scan-1", { request_id: "l1", label: { location: "ピット", tags: [], memo: "" }, edited_at: 1 }));
    expect(n).toBeLessThanOrEqual(5);
    [, n] = await e.count(() => e.app("POST", "/api/app/records", { request_id: "w1", record: { id: "walk-1", kind: "walk", created_at: 1, stations: [] } }));
    expect(n).toBeLessThanOrEqual(5);
    [, n] = await e.count(() => e.pc("PUT", "/api/pc/records/scan-1", { record: { id: "scan-1", kind: "pc_scan", created_at: 1, stations: [] } }));
    expect(n).toBeLessThanOrEqual(5);
    [, n] = await e.count(() => e.app("GET", "/api/app/records?since=0"));
    expect(n).toBeLessThanOrEqual(2);
    [, n] = await e.count(() => e.app("GET", "/api/app/records/scan-1"));
    expect(n).toBeLessThanOrEqual(2);
  });
});

describe("指示", () => {
  it("アプリ → 中継 → PC、PCが保存したと返すまで消さない（受け取って落ちても失わない）", async () => {
    const e = env();
    await e.app("POST", "/api/app/commands", scan("r1"));
    const a = await e.pc("POST", "/api/pc/sync", { version: null });
    expect(a.body.inbox).toHaveLength(1);
    expect(a.body.inbox[0]).toMatchObject({ kind: "command", type: "scan", request_id: "r1", field: "cmd:scan" });
    // PCが保存する前に落ちた → 起動後は版数なしで問い合わせる → もう一度届く
    const b = await e.pc("POST", "/api/pc/sync", { version: null });
    expect(b.body.inbox).toHaveLength(1);
    // 保存したと返すと消える
    await e.pc("POST", "/api/pc/sync", { version: b.body.version, acks: ["cmd:scan"] });
    const c = await e.pc("POST", "/api/pc/sync", { version: null });
    expect(c.body.inbox).toHaveLength(0);
  });

  it("同じ要求IDを二重に送っても結果は同じ", async () => {
    const e = env();
    const a = await e.app("POST", "/api/app/commands", scan("r1"));
    const b = await e.app("POST", "/api/app/commands", scan("r1"));
    expect(b.status).toBe(200);
    expect(b.body.duplicate).toBe(true);
    expect(b.body.accepted_at).toBe(a.body.accepted_at);
    const s = await e.pc("POST", "/api/pc/sync", { version: null });
    expect(s.body.inbox).toHaveLength(1);
  });

  it("PCがまだ受け取っていない手動スキャンがあれば次の手動スキャンは断る", async () => {
    const e = env();
    await e.app("POST", "/api/app/commands", scan("r1"));
    const b = await e.app("POST", "/api/app/commands", scan("r2"));
    expect(b.status).toBe(409);
    expect(b.body.error).toBe("busy");
    // 期限を過ぎていれば置き換えられる
    e.advance(61_000);
    const c = await e.app("POST", "/api/app/commands", scan("r3"));
    expect(c.status).toBe(200);
  });

  it("60秒を過ぎた指示はPCに渡さずに消す", async () => {
    const e = env();
    await e.app("POST", "/api/app/commands", { request_id: "b1", type: "bands", params: { excluded_bands: ["2.4"] } });
    e.advance(60_001);
    const s = await e.pc("POST", "/api/pc/sync", { version: null });
    expect(s.body.inbox).toHaveLength(0);
    expect(await e.redis.hgetall("ww:inbox")).toEqual({});
  });

  it("中継が受けるのは scan と bands だけ、値も検査する（検査に落ちたらRedisを使わない）", async () => {
    const e = env();
    const bad = [
      { request_id: "x1", type: "mode", params: { mode: "PRE" } },
      { request_id: "x2", type: "exec", params: { cmd: "rm -rf /" } },
      { request_id: "x3", type: "scan", params: { bands: ["7"] } },
      { request_id: "x4", type: "scan", params: { freqs: [6136] } },
      { request_id: "x5", type: "bands", params: { excluded_freqs: Array.from({ length: 58 }, () => 5955) } },
      { request_id: "bad id!", type: "scan", params: {} },
    ];
    for (const b of bad) expect((await e.app("POST", "/api/app/commands", b)).status).toBe(400);
    expect(e.redis.usage.commands).toBe(0);
  });

  it("大きすぎる本文は断る", async () => {
    const e = env();
    const big = { request_id: "w1", record: { id: "w1", kind: "walk", created_at: 1, stations: [], pad: "x".repeat(600 * 1024) } };
    expect((await e.app("POST", "/api/app/records", big)).status).toBe(413);
    const st = { version: null, state: { pad: "x".repeat(9000) } };
    expect((await e.pc("POST", "/api/pc/sync", st)).status).toBe(413);
  });

  it("Redisが空に戻っても取りこぼさない（版数は「違うか」で比べる）", async () => {
    const e = env();
    for (let i = 0; i < 5; i++) await e.app("POST", "/api/app/poll", { opened: true });
    const a = await e.pc("POST", "/api/pc/sync", { version: null });
    expect(a.body.version).toBe("5");
    e.redis.flushAll();
    await e.app("POST", "/api/app/commands", scan("r9"));
    const b = await e.pc("POST", "/api/pc/sync", { version: a.body.version });
    expect(b.body.version).toBe("1");
    expect(b.body.changed).toBe(true);
    expect(b.body.inbox).toHaveLength(1);
  });
});

describe("ラベル・歩測・記録の写し", () => {
  it("同じ記録のラベルを続けて変えても、どちらも消えない", async () => {
    const e = env();
    await e.app("PATCH", "/api/app/records/scan-1", { request_id: "l1", label: { location: "ピット" }, edited_at: 100 });
    await e.app("PATCH", "/api/app/records/scan-1", { request_id: "l2", label: { location: "客席東" }, edited_at: 200 });
    const s = await e.pc("POST", "/api/pc/sync", { version: null });
    expect(s.body.inbox.map((i: { request_id: string }) => i.request_id)).toEqual(["l1", "l2"]);
    // l1だけ保存した → l2は残る
    await e.pc("POST", "/api/pc/sync", { version: s.body.version, acks: ["lbl:scan-1:l1"] });
    const t = await e.pc("POST", "/api/pc/sync", { version: null });
    expect(t.body.inbox.map((i: { request_id: string }) => i.request_id)).toEqual(["l2"]);
  });

  it("ラベルの値を検査する", async () => {
    const e = env();
    expect((await e.app("PATCH", "/api/app/records/scan-1", { request_id: "l1", label: { memo: "x".repeat(1001) }, edited_at: 1 })).status).toBe(400);
    expect((await e.app("PATCH", "/api/app/records/scan-1", { request_id: "l1", label: {} })).status).toBe(400);
  });

  it("歩測の記録は同じIDなら1件（二重に届いても同じ）", async () => {
    const e = env();
    const w = { request_id: "w1", record: { id: "walk-1", kind: "walk", created_at: 1, stations: [{ bssid: "aa:bb:cc:dd:ee:ff", freq: 5955 }] } };
    await e.app("POST", "/api/app/records", w);
    await e.app("POST", "/api/app/records", w);
    const s = await e.pc("POST", "/api/pc/sync", { version: null });
    expect(s.body.inbox).toHaveLength(1);
    expect((await e.app("POST", "/api/app/records", { ...w, record: { ...w.record, kind: "pc_scan" } })).status).toBe(400);
  });

  it("PCが置いた記録の写しを、アプリが一覧と詳細で読める（更新の差分だけ）", async () => {
    const e = env();
    for (const i of [1, 2, 3]) {
      e.advance(1000);
      await e.pc("PUT", `/api/pc/records/scan-${i}`, { record: { id: `scan-${i}`, kind: "pc_scan", created_at: i, bands: ["6"], stations: [{}, {}], label: { location: null, tags: [], memo: "" } } });
    }
    const l = await e.app("GET", "/api/app/records?since=0");
    expect(l.body.records.map((r: { id: string }) => r.id)).toEqual(["scan-1", "scan-2", "scan-3"]);
    expect(l.body.records[0].n_stations).toBe(2);
    e.advance(1000);
    await e.pc("PUT", "/api/pc/records/scan-2", { record: { id: "scan-2", kind: "pc_scan", created_at: 2, stations: [], label: { location: "ピット", tags: [], memo: "" } } });
    const d = await e.app("GET", `/api/app/records?since=${l.body.next_since}`);
    expect(d.body.records.map((r: { id: string }) => r.id)).toEqual(["scan-2"]);
    expect(d.body.records[0].label.location).toBe("ピット");
    const one = await e.app("GET", "/api/app/records/scan-2");
    expect(one.body.id).toBe("scan-2");
    expect((await e.app("GET", "/api/app/records/none")).status).toBe(404);
    expect((await e.pc("PUT", "/api/pc/records/scan-9", { record: { id: "other" } })).status).toBe(400);
  });

  it("状態の写しは中継の時計で古さを出す", async () => {
    const e = env();
    await e.pc("POST", "/api/pc/sync", { version: null, state: { mode: "QUIET" } });
    e.advance(7000);
    const p = await e.app("POST", "/api/app/poll", {});
    expect(p.body.state).toEqual({ mode: "QUIET" });
    expect(p.body.state_age_ms).toBe(7000);
    // 前面の印は10秒で消える
    e.advance(10_001);
    const s = await e.pc("POST", "/api/pc/sync", { version: "0" });
    expect(s.body.fg).toBe(false);
  });
});

describe("PCが今見ている局（アプリが頼んだときだけ）", () => {
  it("頼む → PCは次の問い合わせで気づく（待機中のコマンド数は1のまま）→ 1回置く → アプリが読む", async () => {
    const e = env();
    const v = (await e.pc("POST", "/api/pc/sync", { version: null })).body.version;
    let [r, n] = await e.count(() => e.pc("POST", "/api/pc/sync", { version: v }));
    expect(n).toBe(1);
    expect(r.body.view_req).toBeNull(); // 頼まれていなければ何もしない（常には配らない）
    [r, n] = await e.count(() => e.app("POST", "/api/app/pcview", {}));
    expect(n).toBe(1);
    const asked = r.body.requested_at;
    [r, n] = await e.count(() => e.pc("POST", "/api/pc/sync", { version: v }));
    expect(n).toBe(1); // 頼みは読むのと同じ1回のMGETに入る
    expect(r.body.view_req).toBe(asked);
    expect(r.body.changed).toBe(false); // 受信箱（指示）には入れない
    [r, n] = await e.count(() => e.pc("PUT", "/api/pc/view", { for: asked, view: { stations: [{ b: "02:00:00:00:00:01", r: -60 }] } }));
    expect(n).toBe(1);
    [r, n] = await e.count(() => e.app("GET", "/api/app/pcview"));
    expect(n).toBe(1);
    expect(r.body.for).toBe(asked);
    expect(r.body.view.stations[0].r).toBe(-60);
    // 頼みは60秒で消える：PCはそれ以上置かない
    e.advance(60_001);
    expect((await e.pc("POST", "/api/pc/sync", { version: v })).body.view_req).toBeNull();
  });

  it("役割違いは断る（アプリは置けない、PCは頼めない）。大きすぎる一覧は413", async () => {
    const e = env();
    expect((await e.raw("PUT", "/api/pc/view", APP, { for: 1, view: {} })).status).toBe(403);
    expect((await e.raw("POST", "/api/app/pcview", PC, {})).status).toBe(403);
    expect((await e.raw("GET", "/api/app/pcview", null, undefined)).status).toBe(401);
    const big = { for: 1, view: { pad: "x".repeat(300 * 1024) } };
    expect((await e.pc("PUT", "/api/pc/view", big)).status).toBe(413);
    expect((await e.pc("PUT", "/api/pc/view", { view: {} })).status).toBe(400);
  });
});
