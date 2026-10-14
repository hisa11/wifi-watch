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

## M1 解析（2026-10-14）

やったこと
- 解析を `testdata/` と照合（IE 23件、フレーム 12件）。独立した解析器（tcpdump）でもradiotap（アンテナ別の強さ、ベンダ名前空間）とSSID・chの読みが一致することを確かめるテストを置いた（合成フレームを一時ファイルに書いて読ませ、すぐ消す）
- `analyzer/stations.py`：局の表（BSSIDで1件、主chはHE Operation、受信したchは別に数える、chの移動の検出、IEの全部の解析は5秒に1回）、端末の表（ヘッダだけ）
- `analyzer/stats.py`：chごとの統計（(主ch, 受信の幅) ごと、10秒の桶、受信秒数で割る、局の主chで数えたビーコン）
- `analyzer/core.py`：フレームの束の振り分け（ch違い・FCS不良を捨てる、自分のフレームを活動量から除く、deauth/disassocの記録）
- `radio/bpf.py`：カーネル内でデータフレームをMACヘッダまでに切り詰めるclassic BPFと、テスト用の解釈器
- `tools/bench.py`：処理能力の測定

テスト
- PC側 83件 成功（M0の31件を含む）

処理能力（`tools/bench.py`、160MHz受信を想定した混ぜ方：ビーコン200・データ2400・ACK2400／秒）
- 開発PC（Ryzen 7 7840HS、Python 3.12.3）で 約12万フレーム/秒（1コア）。目安の毎秒5000の約24倍。ミニPCでの値はM7で測る。

## M2 巡回（2026-10-15）

やったこと
- `scheduler.py`：PRE/LIVE/QUIETの計画、枠の割り当てと配り直し、予備chの専用枠、手動スキャン（1つだけ、QUIETは確認、終われば元のモード）、帯域・chのON/OFF（自ブロックは外せない）、初回起動の先回り、生存確認の枠と「周期」、chごとの見え方（own/in_block/patrol/excluded/scanning/paused）と再訪の間隔
- `engine.py`：受信ループ（tune → capture → 解析、長い枠は0.5秒ずつ、同じ設定ならtuneしない、定期処理の口）
- `scan.py`：手動スキャンの結果を記録（局の表、chごとの占有）にまとめる

テスト
- PC側 98件 成功（M2で15件）

仕様書の表との照合（偽の時計でLIVE 600秒、QUIET 300秒）
| 項目 | 仕様書 | 実測 |
| --- | --- | --- |
| LIVEの自chの受信率 | 70% | 割り当て70.0%、切替を除いた受信68.8% |
| 自ブロック内の別chの再訪（最大） | 約20秒 | 17.75秒 |
| 6GHzのその他 | 40秒 | 40.0秒 |
| 5GHz | 約70秒 | 67.5秒 |
| 2.4GHz | 130秒 | 130.0秒 |
| QUIETの自chの受信率 | 約89% | 割り当て89.2%、実測88.0% |
| PREの1周 | 約14秒 | 14.25秒 |
| 手動スキャン（全57ch／6GHzだけ） | 約14秒／約6秒 | 14.25秒／6.0秒 |
| 予備ch（5GHz）の再訪 | 10秒 | 10.0秒（他の5GHzは最大97秒） |

まだのもの（後のマイルストーンでつなぐ）
- 帯域ON/OFFを再起動後も残す（`Overrides` はM0で用意済み。controllerでつなぐのはM5）、変更をイベントに残す（M5）
- 生存確認のchの選び方（M3）

## 次にやること

M3 警報：警報の枠組み、表の全警報、生存確認と復旧、分類（署名・チーム・OUI・機種推定・RNR）。
