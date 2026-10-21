// pm2 の設定（仕様書「sudo pm2 で常駐させる」）。2026-10-21 からは使わない：本番も ./start.sh pit で起動し、Ctrl-C で止める
// （常駐させると止めても再起動しても無線が戻らなかった。docs/DECISIONS.md）。常駐させたいときだけ使う。
//   sudo WIFIWATCH_USER=<ユーザー名> pm2 start deploy/ecosystem.config.js
// radiod（無線の補助）は root、本体（解析・API・中継・通知）は普通のユーザーで動かす。
const path = require("path");
const fs = require("fs");

const root = path.resolve(__dirname, "..");
const py = path.join(root, "server", ".venv", "bin", "python");
const cfg = JSON.parse(fs.readFileSync(path.join(root, "config.json"), "utf8"));
const user = process.env.WIFIWATCH_USER || process.env.SUDO_USER;
if (!user) throw new Error("WIFIWATCH_USER（本体を動かすユーザー）を指定してください");
// 本番の常駐で偽の無線（config.example.json の既定）を動かさない。試しに偽の無線で常駐させるときだけ WIFIWATCH_ALLOW_FAKE=1
const kind = (cfg.radio || {}).kind || "fake";
if (kind === "fake" && process.env.WIFIWATCH_ALLOW_FAKE !== "1") {
  throw new Error('config.json の radio.kind が "fake"（偽の無線）です。本番は "radiod" にしてください（deploy/setup.sh で直せます）');
}
const iface = cfg.interface || "wlan0";
if (!/^[A-Za-z0-9_.-]{1,15}$/.test(iface)) throw new Error("interface が不正です");
const flags = ((cfg.radio || {}).monitor_flags || []).flatMap((f) => ["--monitor-flag", f]);
const scanOnly = process.env.WIFIWATCH_SCAN_ONLY === "1" ? ["--scan-only"] : [];
const sock = (cfg.radio || {}).radiod_socket || "/run/wifiwatch/radiod.sock";

module.exports = {
  apps: [
    {
      name: "wifiwatch-radiod",
      cwd: path.join(root, "server"),
      script: py,
      interpreter: "none",
      args: ["-m", "wifiwatch.radio.radiod", "--interface", iface, "--socket", sock, "--group", "wifiwatch", ...flags, ...scanOnly],
      autorestart: true,
      restart_delay: 2000,
      env: { PYTHONUNBUFFERED: "1" },
    },
    {
      name: "wifiwatch",
      cwd: path.join(root, "server"),
      script: py,
      interpreter: "none",
      args: ["-m", "wifiwatch", "--config", path.join(root, "config.json")],
      uid: user,
      gid: "wifiwatch",
      autorestart: true,
      restart_delay: 2000,
      env: { PYTHONUNBUFFERED: "1" },
    },
  ],
};
