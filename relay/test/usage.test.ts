// 無料枠の見積もり：偽のRedisが数えたコマンド数と転送量から、仕様書の2つの場面の月の使用量を出す。
// 仕様書の見込み（Upstash：大会4日でコマンド約29%・30日つけっぱなしで約52%、帯域約4%）を超えないことを確かめる。
import { describe, expect, it } from "vitest";
import { env } from "./helpers.js";

const FREE_COMMANDS = 500_000; // 月（2026-10-14 確認）
const FREE_BYTES = 10 * 1024 ** 3; // 帯域10GB/月

/** 8KB近い状態の写し（帯域の見込みの前提：1回8KB以下） */
function bigState() {
  const s = { mode: "LIVE", pad: "" };
  const target = 8 * 1024 - 64;
  s.pad = "x".repeat(target - JSON.stringify(s).length);
  return s;
}

/** 1日ぶんを2秒刻みで回す。fgHours：アプリを開いている時間、onHours：PCが動いている時間 */
async function simulateDay(onHours: number, fgHours: number, opens: number, scans: number) {
  const e = env();
  const state = bigState();
  let version: string | null = null;
  let fg = false;
  const total = onHours * 3600;
  const fgStart = total - fgHours * 3600; // 簡単のため最後にまとめて開く（回数は opens 回に分ける）
  const fgChunk = (fgHours * 3600) / Math.max(opens, 1);
  let nextPc = 0;
  let scanEvery = scans ? Math.floor(total / scans) : Infinity;
  let pending: string[] = [];
  for (let t = 0; t < total; t += 2) {
    const inFg = fgHours > 0 && t >= fgStart;
    if (inFg) {
      const opened = (t - fgStart) % fgChunk < 2;
      await e.app("POST", "/api/app/poll", { opened });
    }
    if (t >= nextPc) {
      const body: Record<string, unknown> = { version, acks: pending };
      if (fg) body.state = state;
      const r = await e.pc("POST", "/api/pc/sync", body);
      pending = (r.body.inbox ?? []).map((i: { field: string }) => i.field);
      version = r.body.version;
      fg = r.body.fg;
      nextPc = t + (fg ? 2 : 10);
    }
    if (scans && t % scanEvery === 0 && t > 0) {
      // 手動スキャン1回：アプリが指示 → PCが記録（約20KB）を置く → アプリが一覧と詳細を読む
      await e.app("POST", "/api/app/commands", { request_id: `s${t}`, type: "scan", params: { bands: ["6"] } });
      const rec = { id: `scan-${t}`, kind: "pc_scan", created_at: t, stations: Array.from({ length: 60 }, (_, i) => ({ bssid: `02:00:00:00:00:${(i % 99).toString().padStart(2, "0")}`, ssid: "S".repeat(16), freq: 5955, width: 20, rssi: -60, standard: "ax" })) };
      await e.pc("PUT", `/api/pc/records/${rec.id}`, { record: rec });
      await e.app("GET", "/api/app/records?since=0&limit=50");
      await e.app("GET", `/api/app/records/${rec.id}`);
    }
    e.advance(2000);
  }
  return e.redis.usage;
}

describe("無料枠の見積もり", () => {
  it("大会4日（1日14時間稼働、アプリを開くのは1日3時間）", async () => {
    const day = await simulateDay(14, 3, 6, 30);
    const cmds = day.commands * 4;
    const bytes = day.bytes * 4;
    const pc = (cmds / FREE_COMMANDS) * 100;
    const bw = (bytes / FREE_BYTES) * 100;
    console.log(`[大会4日] コマンド ${cmds}（月の枠の${pc.toFixed(1)}%）、転送 ${(bytes / 1024 ** 2).toFixed(0)}MB（${bw.toFixed(1)}%）、内訳/日 ${JSON.stringify(day.byCommand)}`);
    expect(pc).toBeLessThanOrEqual(29);
    expect(bw).toBeLessThanOrEqual(4);
  }, 60_000);

  it("30日つけっぱなし（アプリは開かない）", async () => {
    const day = await simulateDay(24, 0, 0, 0);
    const cmds = day.commands * 30;
    const bytes = day.bytes * 30;
    const pc = (cmds / FREE_COMMANDS) * 100;
    console.log(`[30日つけっぱなし] コマンド ${cmds}（月の枠の${pc.toFixed(1)}%）、転送 ${(bytes / 1024 ** 2).toFixed(0)}MB（${((bytes / FREE_BYTES) * 100).toFixed(1)}%）`);
    expect(pc).toBeLessThanOrEqual(52);
  }, 60_000);
});
