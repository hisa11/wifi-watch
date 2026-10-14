# 進み具合

新しいセッションは、いちばん下の節の「次にやること」から始める。

## M0 土台（2026-10-14）

やったこと
- リポジトリ、`CLAUDE.md`（依頼書3章の写し）、`docs/spec.md`（仕様書）、`docs/brief.md`（依頼書）、`PLAN.md`、`DECISIONS.md`
- `server/`：venv（`server/.venv`）、依存の版の固定（`requirements.txt`、`requirements-dev.txt`）
  - `channels.py`：日本の57ch（MHz）、PSC、40/80/160/320MHzのブロック、重なり、設定のchの読み方
  - `clock.py`：実時間と偽の時計、UTC保存・JST表示
  - `config.py`：`config.json` の検証（日本語で場所と理由を出して止める）、`overrides.json`
  - `capture/`：radiotap・802.11ヘッダ・IE（HE/EHT Operation、RNR、RSN、WPS…）の解析と、主ch・幅の決定、合成フレームの生成器（`build.py`）
  - `radio/base.py`・`radio/fake.py`：無線の共通インタフェースと偽の無線（シナリオの局が合成フレームを出す。故障の注入あり）
  - `tools/gen_testdata.py` → `testdata/`（IE 23件、フレーム 12件、chの表）
- `relay/`：TypeScript、`api/index.ts`（Vercelの薄い層）、`src/handler.ts`（トークン検査、Redisより先）、`src/redis.ts`（Upstashの薄い層）、`src/fake-redis.ts`（コマンド数と転送量を数える偽物）、`src/dev-server.ts`（ローカルの中継）、`vercel.json`（hnd1）
- リポジトリ直下：`config.example.json`、`signatures.json`、`.gitignore`

テスト
- PC側 31件 成功、中継 2件 成功、`tsc` の型検査 成功

## 次にやること

M1 解析：radiotap・ヘッダ・IEの解析を `testdata/` と照合するテスト、局の表、chごとの統計、BPFでの切り詰め、処理能力の測定。
