// testdata/relay の見本（PC側・中継・アプリで共用）を中継が受け付けるか。
import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { env } from "./helpers.js";

const load = (n: string) => JSON.parse(readFileSync(new URL(`../../testdata/relay/${n}`, import.meta.url), "utf8"));

describe("見本", () => {
  it("アプリの見本はすべて受け付け、PCに同じ形で届く", async () => {
    const e = env();
    const scan = load("app_command_scan.json");
    const bands = load("app_command_bands.json");
    const label = load("app_label.json");
    const walk = load("app_walk.json");
    expect((await e.app("POST", "/api/app/commands", scan)).status).toBe(200);
    expect((await e.app("POST", "/api/app/commands", bands)).status).toBe(200);
    expect((await e.app("PATCH", `/api/app/records/${walk.record.id}`, label)).status).toBe(200);
    expect((await e.app("POST", "/api/app/records", walk)).status).toBe(200);
    const s = await e.pc("POST", "/api/pc/sync", { version: null });
    const kinds = s.body.inbox.map((i: { kind: string }) => i.kind).sort();
    expect(kinds).toEqual(["command", "command", "label", "walk"]);
    const w = s.body.inbox.find((i: { kind: string }) => i.kind === "walk");
    expect(w.record).toEqual(walk.record);
  });

  it("PCの見本（状態の写し・記録の写し）を受け付け、アプリが読める", async () => {
    const e = env();
    const state = load("pc_state.json");
    const rec = load("pc_record.json");
    expect((await e.pc("POST", "/api/pc/sync", { version: null, state })).status).toBe(200);
    expect((await e.pc("PUT", `/api/pc/records/${rec.id}`, { record: rec })).status).toBe(200);
    const p = await e.app("POST", "/api/app/poll", {});
    expect(p.body.state).toEqual(state);
    const poll = load("app_poll_response.json");
    expect(Object.keys(p.body).sort()).toEqual(Object.keys(poll).sort());
    const one = await e.app("GET", `/api/app/records/${rec.id}`);
    expect(one.body.stations).toEqual(rec.stations);
  });
});
