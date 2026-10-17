// 中継のAPI。Redisの持ち方とコマンド数の上限は docs/relay.md を参照。
//
// キー（すべて ww: で始まる）
//   ww:ver        版数（アプリが受信箱に置いたとき・アプリを開いたときに1増やす）
//   ww:fg         アプリが前面にいる印（期限10秒。アプリの問い合わせのたびに延ばす）
//   ww:inbox      受信箱（ハッシュ）。PCが「保存した」と返すまで消さない
//                   cmd:scan                 手動スキャン（同時に1つだけ）
//                   cmd:<要求ID>             帯域ON/OFF
//                   lbl:<記録ID>:<要求ID>    ラベルの変更（編集ごとに別の項目。新しい変更を上書きで消さない）
//                   walk:<記録ID>            歩測の記録
//   ww:state      PCの状態の写し（中継が受け取った時刻つき）
//   ww:rec:<id>   記録の写し（本文）
//   ww:recmeta    記録の一覧用の情報（ハッシュ id → JSON）
//   ww:recupd     更新時刻の索引（ソート済み集合、点数＝中継の時刻ms）
//   ww:recbytes   記録の写しの合計バイト数
//   ww:viewreq    アプリが「PCが今見ている局を見たい」と頼んだ時刻（期限60秒）。指示ではない（PCの動きは変えない）
//   ww:view       PCが頼まれたときだけ置く、PCが今見ている局の一覧（期限1時間）
//
// 1回の問い合わせのコマンド数（上限）
//   PCの待機中 1 ／ PCのアプリ前面中 4 ／ アプリの2秒ごとの問い合わせ 2（開いた直後は3）／ 置く操作 5以内
//   PCの見ている局：頼む 1 ／ PCが置く 1 ／ アプリが読む 1

import { HttpError, error, json, readJson, type Deps, type Route } from "./handler.js";
import { makeHandler } from "./handler.js";

export const K = {
  ver: "ww:ver",
  fg: "ww:fg",
  inbox: "ww:inbox",
  state: "ww:state",
  rec: (id: string) => `ww:rec:${id}`,
  recmeta: "ww:recmeta",
  recupd: "ww:recupd",
  recbytes: "ww:recbytes",
  viewreq: "ww:viewreq",
  view: "ww:view",
};

export const LIMITS = {
  commandTtlMs: 60_000,
  fgTtlS: 10,
  stateBytes: 8 * 1024,
  syncBody: 24 * 1024,
  smallBody: 8 * 1024,
  recordBody: 512 * 1024,
  recordsTotalBytes: 100 * 1024 * 1024,
  listMax: 500,
  viewReqTtlS: 60,
  viewTtlS: 3600,
  viewBody: 256 * 1024,
};

const BANDS = ["2.4", "5", "6"] as const;
const FREQS = new Set<number>([
  ...Array.from({ length: 13 }, (_, i) => 2412 + 5 * i),
  ...[36, 40, 44, 48, 52, 56, 60, 64, 100, 104, 108, 112, 116, 120, 124, 128, 132, 136, 140, 144].map((c) => 5000 + 5 * c),
  ...Array.from({ length: 24 }, (_, i) => 5955 + 20 * i),
]);
const ID_RE = /^[A-Za-z0-9_-]{1,80}$/;

// ------------------------------------------------------------------ 検査
function obj(v: unknown, what: string): Record<string, unknown> {
  if (!v || typeof v !== "object" || Array.isArray(v)) throw new HttpError(400, "bad_request", `${what} はオブジェクトです`);
  return v as Record<string, unknown>;
}
function id(v: unknown, what: string): string {
  if (typeof v !== "string" || !ID_RE.test(v)) throw new HttpError(400, "bad_request", `${what} が不正です（英数字と _ - の80文字以内）`);
  return v;
}
function bands(v: unknown): string[] {
  if (!Array.isArray(v) || v.length > 3 || !v.every((b) => typeof b === "string" && (BANDS as readonly string[]).includes(b)))
    throw new HttpError(400, "bad_bands", "帯域は 2.4 / 5 / 6 の並びです");
  return [...new Set(v as string[])];
}
function freqs(v: unknown): number[] {
  if (!Array.isArray(v) || v.length > 57 || !v.every((f) => Number.isInteger(f) && FREQS.has(f as number)))
    throw new HttpError(400, "bad_freqs", "chは日本で使える57chの周波数（MHz）です");
  return [...new Set(v as number[])];
}
function label(v: unknown): Record<string, unknown> {
  const l = obj(v, "label");
  const loc = l.location;
  if (loc !== undefined && loc !== null && (typeof loc !== "string" || loc.length > 40)) throw new HttpError(400, "bad_label", "場所は40文字以内です");
  const tags = l.tags ?? [];
  if (!Array.isArray(tags) || tags.length > 10 || !tags.every((t) => typeof t === "string" && t.length >= 1 && t.length <= 24))
    throw new HttpError(400, "bad_label", "タグは10個まで、1つ24文字までです");
  const memo = l.memo ?? "";
  if (typeof memo !== "string" || memo.length > 1000) throw new HttpError(400, "bad_label", "メモは1000文字以内です");
  return { location: loc ?? null, tags, memo };
}
function parse<T>(s: string | null): T | null {
  if (s === null) return null;
  try {
    return JSON.parse(s) as T;
  } catch {
    return null;
  }
}
const bytes = (s: string) => Buffer.byteLength(s);

interface InboxItem {
  kind: "command" | "label" | "walk";
  accepted_at: number;
  request_id: string;
  [k: string]: unknown;
}

// ------------------------------------------------------------------ アプリ用
async function appPoll(req: Request, d: Deps): Promise<Response> {
  const b = obj((await readJson(req, LIMITS.smallBody)) ?? {}, "本文");
  const r = await d.redis();
  const now = d.now();
  await r.set(K.fg, String(now), { ex: LIMITS.fgTtlS });
  if (b.opened === true) await r.incr(K.ver); // 開いた直後：PCが10秒以内に気づいて2秒間隔に入る
  const raw = await r.get(K.state);
  const st = parse<{ state: unknown; received_at: number }>(raw);
  return json(200, {
    now,
    state: st?.state ?? null,
    state_received_at: st?.received_at ?? null,
    state_age_ms: st ? now - st.received_at : null,
  });
}

async function appCommand(req: Request, d: Deps): Promise<Response> {
  const b = obj(await readJson(req, LIMITS.smallBody), "本文");
  const rid = id(b.request_id, "request_id");
  const type = b.type;
  const p = obj(b.params ?? {}, "params");
  let params: Record<string, unknown>;
  if (type === "scan") {
    params = { confirm: p.confirm === true };
    if (p.bands !== undefined) params.bands = bands(p.bands);
    if (p.freqs !== undefined) params.freqs = freqs(p.freqs);
  } else if (type === "bands") {
    params = { excluded_bands: bands(p.excluded_bands ?? []), excluded_freqs: freqs(p.excluded_freqs ?? []) };
  } else {
    // モードの切替・設定の書き換え・任意のコマンドは受けない
    throw new HttpError(400, "unsupported", "中継が受ける指示は scan と bands だけです");
  }
  const r = await d.redis();
  const now = d.now();
  const item: InboxItem = { kind: "command", type, params, request_id: rid, accepted_at: now };
  const field = type === "scan" ? "cmd:scan" : `cmd:${rid}`;
  const val = JSON.stringify(item);
  const added = await r.hsetnx(K.inbox, field, val);
  if (!added) {
    const prev = parse<InboxItem>(await r.hget(K.inbox, field));
    if (prev && prev.request_id === rid) {
      return json(200, { id: rid, accepted_at: prev.accepted_at, now, duplicate: true });
    }
    if (type === "scan" && prev && now - prev.accepted_at <= LIMITS.commandTtlMs) {
      return error(409, "busy", "PCがまだ受け取っていない手動スキャンがあります");
    }
    await r.hset(K.inbox, { [field]: val }); // 期限切れの古いものは置き換える
  }
  await r.incr(K.ver); // 置いてから版数を上げる（逆だとPCが取りこぼす）
  return json(200, { id: rid, accepted_at: now, now, expires_at: now + LIMITS.commandTtlMs });
}

async function appLabel(req: Request, d: Deps, prm: Record<string, string>): Promise<Response> {
  const recId = id(prm.id, "記録のID");
  const b = obj(await readJson(req, LIMITS.smallBody), "本文");
  const rid = id(b.request_id, "request_id");
  const lab = label(b.label);
  const edited = b.edited_at;
  if (typeof edited !== "number" || !Number.isFinite(edited)) throw new HttpError(400, "bad_time", "edited_at（UTCのエポック秒）が要ります");
  const r = await d.redis();
  const now = d.now();
  const item: InboxItem = { kind: "label", record_id: recId, label: lab, edited_at: edited, request_id: rid, accepted_at: now };
  await r.hset(K.inbox, { [`lbl:${recId}:${rid}`]: JSON.stringify(item) });
  await r.incr(K.ver);
  return json(200, { id: rid, accepted_at: now, now });
}

async function appWalk(req: Request, d: Deps): Promise<Response> {
  const b = obj(await readJson(req, LIMITS.recordBody), "本文");
  const rid = id(b.request_id, "request_id");
  const rec = obj(b.record, "record");
  const recId = id(rec.id, "record.id");
  if (rec.kind !== "walk") throw new HttpError(400, "bad_record", "アプリから置けるのは歩測の記録（kind=walk）だけです");
  if (typeof rec.created_at !== "number") throw new HttpError(400, "bad_record", "record.created_at が要ります");
  if (!Array.isArray(rec.stations) || rec.stations.length > 2000) throw new HttpError(400, "bad_record", "record.stations が不正です");
  if (rec.label !== undefined) label(rec.label);
  const r = await d.redis();
  const now = d.now();
  const item: InboxItem = { kind: "walk", record: rec, request_id: rid, accepted_at: now };
  await r.hset(K.inbox, { [`walk:${recId}`]: JSON.stringify(item) }); // 同じ記録は同じ項目＝二重に届いても同じ
  await r.incr(K.ver);
  return json(200, { id: recId, accepted_at: now, now });
}

// PCが今見ている局（アプリが頼んだときだけ）。受信箱には入れない：PCの動きを変える指示ではなく、
// 「前面にいる印」と同じく、PCが次の問い合わせで気づいて一覧を1回置くだけ
async function appViewRequest(req: Request, d: Deps): Promise<Response> {
  obj((await readJson(req, LIMITS.smallBody)) ?? {}, "本文");
  const r = await d.redis();
  const now = d.now();
  await r.set(K.viewreq, String(now), { ex: LIMITS.viewReqTtlS }); // 1
  return json(200, { requested_at: now, now });
}

async function appView(_req: Request, d: Deps): Promise<Response> {
  const r = await d.redis();
  const v = parse<{ view: unknown; for: number; received_at: number }>(await r.get(K.view)); // 1
  const now = d.now();
  return json(200, { now, view: v?.view ?? null, for: v?.for ?? null, received_at: v?.received_at ?? null });
}

async function appRecords(req: Request, d: Deps): Promise<Response> {
  const u = new URL(req.url);
  const since = Number(u.searchParams.get("since") ?? "0");
  const limit = Math.max(1, Math.min(LIMITS.listMax, Number(u.searchParams.get("limit") ?? "200")));
  if (!Number.isFinite(since)) throw new HttpError(400, "bad_request", "since が不正です");
  const r = await d.redis();
  const ids = await r.zrangeByScore(K.recupd, since + 1, Number.MAX_SAFE_INTEGER, limit);
  const metas = ids.length ? await r.hmget(K.recmeta, ids) : [];
  const records = metas.map((m) => parse<Record<string, unknown>>(m)).filter((m): m is Record<string, unknown> => !!m);
  const last = records.reduce((a, m) => Math.max(a, Number(m.updated_at ?? 0)), since);
  return json(200, { now: d.now(), records, next_since: last, more: ids.length === limit });
}

async function appRecord(_req: Request, d: Deps, prm: Record<string, string>): Promise<Response> {
  const recId = id(prm.id, "記録のID");
  const r = await d.redis();
  const body = await r.get(K.rec(recId));
  if (body === null) return error(404, "not_found", "その記録はありません（古い順に消えた可能性があります）");
  const meta = parse<Record<string, unknown>>(await r.hget(K.recmeta, recId));
  const rec = parse<Record<string, unknown>>(body) ?? {};
  return json(200, { ...rec, meta });
}

// ------------------------------------------------------------------ PC用
async function pcSync(req: Request, d: Deps): Promise<Response> {
  const b = obj(await readJson(req, LIMITS.syncBody), "本文");
  const version = b.version === null || b.version === undefined ? null : String(b.version);
  const acks = b.acks ?? [];
  if (!Array.isArray(acks) || acks.length > 200 || !acks.every((a) => typeof a === "string" && a.length <= 200))
    throw new HttpError(400, "bad_request", "acks は文字列の並びです");
  let stateStr: string | null = null;
  if (b.state !== undefined) {
    stateStr = JSON.stringify(b.state);
    if (bytes(stateStr) > LIMITS.stateBytes) throw new HttpError(413, "too_large", `状態の写しは${LIMITS.stateBytes}バイト以内です`);
  }
  const r = await d.redis();
  const now = d.now();
  const [ver, fg, viewreq] = await r.mget([K.ver, K.fg, K.viewreq]); // 1
  const current = ver ?? "0";
  const changed = version === null || version !== current;
  let inbox: (InboxItem & { field: string })[] | undefined;
  const drop: string[] = [...(acks as string[])];
  if (changed) {
    const all = await r.hgetall(K.inbox); // 2
    inbox = [];
    for (const [field, raw] of Object.entries(all)) {
      const it = parse<InboxItem>(raw);
      if (!it) {
        drop.push(field);
        continue;
      }
      if (it.kind === "command" && now - it.accepted_at > LIMITS.commandTtlMs) {
        drop.push(field); // 期限を過ぎた指示はPCに渡さずに消す
        continue;
      }
      inbox.push({ ...it, field });
    }
    inbox.sort((a, c) => a.accepted_at - c.accepted_at);
  }
  if (drop.length) await r.hdel(K.inbox, [...new Set(drop)]); // 3
  if (stateStr !== null) await r.set(K.state, JSON.stringify({ state: JSON.parse(stateStr), received_at: now })); // 4
  const vr = viewreq === null ? null : Number(viewreq);
  return json(200, { now, version: current, changed, fg: fg !== null, view_req: Number.isFinite(vr) ? vr : null, inbox });
}

async function pcView(req: Request, d: Deps): Promise<Response> {
  const b = obj(await readJson(req, LIMITS.viewBody), "本文");
  const forMs = b.for;
  if (typeof forMs !== "number" || !Number.isFinite(forMs)) throw new HttpError(400, "bad_request", "for（頼まれた時刻）が要ります");
  const view = obj(b.view, "view");
  const r = await d.redis();
  const now = d.now();
  await r.set(K.view, JSON.stringify({ view, for: forMs, received_at: now }), { ex: LIMITS.viewTtlS }); // 1
  return json(200, { received_at: now });
}

async function pcRecord(req: Request, d: Deps, prm: Record<string, string>): Promise<Response> {
  const recId = id(prm.id, "記録のID");
  const b = obj(await readJson(req, LIMITS.recordBody), "本文");
  const rec = obj(b.record, "record");
  if (rec.id !== recId) throw new HttpError(400, "bad_record", "record.id がパスと違います");
  const body = JSON.stringify(rec);
  const size = bytes(body);
  const r = await d.redis();
  const now = d.now();
  const lab = (rec.label as Record<string, unknown> | undefined) ?? {};
  const meta = {
    id: recId, kind: rec.kind, created_at: rec.created_at, device: rec.device ?? null, origin: rec.origin ?? null,
    bands: rec.bands ?? [], label: lab, n_stations: Array.isArray(rec.stations) ? rec.stations.length : 0,
    updated_at: now, size,
  };
  const old = parse<{ size?: number }>(await r.hget(K.recmeta, recId)); // 1
  await r.set(K.rec(recId), body); // 2
  await r.hset(K.recmeta, { [recId]: JSON.stringify(meta) }); // 3
  await r.zadd(K.recupd, now, recId); // 4
  const total = await r.incrby(K.recbytes, size - (old?.size ?? 0)); // 5
  let trimmed = 0;
  if (total > LIMITS.recordsTotalBytes) trimmed = await trim(d, total); // まれ：合計の上限を超えたら古い順に消す
  return json(200, { id: recId, updated_at: now, total_bytes: total, trimmed });
}

async function trim(d: Deps, total: number): Promise<number> {
  const r = await d.redis();
  const ids = await r.zrangeByScore(K.recupd, 0, Number.MAX_SAFE_INTEGER, 50);
  const metas = await r.hmget(K.recmeta, ids);
  const victims: string[] = [];
  let freed = 0;
  for (let i = 0; i < ids.length && total - freed > LIMITS.recordsTotalBytes * 0.9; i++) {
    victims.push(ids[i]!);
    freed += parse<{ size?: number }>(metas[i] ?? null)?.size ?? 0;
  }
  if (!victims.length) return 0;
  await r.del(victims.map(K.rec));
  await r.hdel(K.recmeta, victims);
  await r.zrem(K.recupd, victims);
  await r.incrby(K.recbytes, -freed);
  return victims.length;
}

export const routes: Route[] = [
  { method: "POST", pattern: /^\/api\/app\/poll$/, role: "app", fn: appPoll },
  { method: "POST", pattern: /^\/api\/app\/commands$/, role: "app", fn: appCommand },
  { method: "GET", pattern: /^\/api\/app\/records$/, role: "app", fn: appRecords },
  { method: "POST", pattern: /^\/api\/app\/records$/, role: "app", fn: appWalk },
  { method: "GET", pattern: /^\/api\/app\/records\/(?<id>[^/]+)$/, role: "app", fn: appRecord },
  { method: "PATCH", pattern: /^\/api\/app\/records\/(?<id>[^/]+)$/, role: "app", fn: appLabel },
  { method: "POST", pattern: /^\/api\/pc\/sync$/, role: "pc", fn: pcSync },
  { method: "PUT", pattern: /^\/api\/pc\/records\/(?<id>[^/]+)$/, role: "pc", fn: pcRecord },
  { method: "POST", pattern: /^\/api\/app\/pcview$/, role: "app", fn: appViewRequest },
  { method: "GET", pattern: /^\/api\/app\/pcview$/, role: "app", fn: appView },
  { method: "PUT", pattern: /^\/api\/pc\/view$/, role: "pc", fn: pcView },
];

export const handle = makeHandler(routes);
