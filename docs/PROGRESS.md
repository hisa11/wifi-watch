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

## M3 警報（2026-10-15）

やったこと
- `classify.py`：署名（`signatures.json`）、チーム対応表（BSSID・SSIDのパターン）、手動タグ、`known_ok_bssids`、OUI（ieee-data）、機種推定（WPS・SSIDの傾向・ベンダIE・規格・暗号・国・BSS Color）、RNRによる2.4/5/6GHzの紐付け（6GHz側にチームとWPSを引き継ぐ）
- `analyzer/alerts.py`：警報の状態（発生・追加・cleared・解消）、30秒の抑制、10秒で解消、1回だけの通知（再起動しても覚える口）
- `analyzer/detect.py`：表の全警報の判定（割当違反2種、新規参入×重なり、正体不明の強力局・広帯域局、RoboconFree・CineEyeの6GHz出現、活動量、リトライ率、同名SSID、deauth洪水、受信停止）
- `liveness.py`：生存確認の周期の判定と復旧（モニタ再設定 → ドライバ読み込み直し・管理モードでスキャン・JPと6GHzの確認・モニタ → 失敗ならcrit、5分ごとに再試行）
- `monitor.py`：部品の組み立て（1秒ごとの判定、生存確認のchは最近いちばん局が多い5GHz）
- `calibration.py`：PREの下見のまとめ

テスト
- PC側 149件 成功（M3で51件：警報37、受信停止8、分類6）
- 完了条件：警報ごとに鳴るべき場面と鳴ってはいけない場面のテストあり。静かな6GHzで受信停止が鳴らない（`test_quiet_6ghz_is_not_receive_stop`）。PRE→LIVEで活動量が鳴らない（`test_pre_to_live_switch_does_not_fire_activity`）。

## M4 通知（2026-10-15）

やったこと
- `store.py`：SQLiteの土台（kv、通知と中継の待ち行列、イベント、記録、処理済みの要求ID、手動タグの表）
- `notify.py`：ntfy（JSONで送る）とDiscord、レベルごとの宛先と優先度、再起動しても消えない待ち行列と無期限の再送（順番を守る、2〜60秒で待つ、429はRetry-After）、稼働中通知（起動時と15分ごと、15分で捨てる）、死活監視へ1分ごと、警報音（`docs/buzzer.md`）
- `tests/fakehttp.py`：偽のHTTPサーバ（127.0.0.1だけ）

テスト
- PC側 159件 成功（M4で10件）
- 完了条件：偽のHTTPサーバを止めて警報を溜め、PCを作り直して（再起動）、サーバを戻すと、全部が入れた順に届く（`test_outage_then_recovery_loses_nothing_even_across_restart`）。偽の無線 → 警報 → ntfy・Discord・警報音まで通る。

## M5 保存・ローカルAPI・画面（2026-10-15）

やったこと
- `store.py`：記録とラベル（編集時刻の新しい方を残す）、処理済みの要求ID、手動タグ、中継に送る待ち行列
- `logs.py`：`events.jsonl`、`channels.csv`（10秒ごと）
- `analyzer/occupancy.py`：chごとの占有（見ているか・最後に受信してから・重なる局・強い局・活動量・予約・問題ch）、回避先の候補
- `controller.py`：ローカルAPIと中継の共通の実行経路（手動スキャン、帯域ON/OFF、モード・自ch・タグ、記録とラベル、歩測の取り込み、書き出し、画面の状態）。要求IDで冪等、変更はイベントに残す
- `api.py`：ローカルAPI（127.0.0.1だけ、Host・独自ヘッダ・Originの検査、CSP）、`docs/api.md`
- `app.py`・`__main__.py`：起動（受信ループと通知を別スレッド、APIは127.0.0.1）
- `static/`：ダッシュボード（上の帯で一目の判断、6GHzのアナライザーが主役、5GHz・2.4GHzの小さな図、警報、自chの活動量、回避先、PREの下見、局・端末・記録・イベント・巡回と手動スキャン・設定のタブ、局の詳細の引き出し、明暗の切替）
- `.claude/launch.json`：偽の無線で画面を動かす設定（`config.json` は `config.example.json` の写し）

テスト
- PC側 181件 成功（M5でAPI 22件）
- 完了条件：偽の無線で起動し、ブラウザ（内蔵ブラウザ、1280×800）で、表示・局の選択と詳細・手動スキャン（進み具合、見ていない範囲の表示）・記録のラベル保存と一覧への反映・巡回のON/OFFの画面を確かめた。CSPの違反は出ていない。Host・独自ヘッダの検査は断る場面と通す場面の両方をテストした。

## M6 中継（2026-10-15）

やったこと
- `relay/src/routes.ts`：アプリ用（poll・commands・records一覧/詳細・ラベル・歩測）とPC用（sync・記録の写し）の口。トークンはRedisより先、受けるのは scan と bands だけ、値の検査、期限60秒、冪等、受信箱はPCのackまで消さない、記録の写しの上限
- `relay/test/`：口ごとのテスト（コマンド数の上限を固定）と、無料枠の見積もり
- `server/wifiwatch/relay_client.py`：中継への問い合わせ（別スレッド、10秒／2秒、起動直後は全部受け取る、controllerで実行、期限の確認、保存できたものだけack、状態の写し、記録の写しを溜めて送る）
- `controller.compact_state()`：8KB以下の状態の写し
- `docs/relay.md`：キーの設計、APIの確定版、コマンド数、見積もり、ローカルの中継、デプロイの手順

テスト
- 中継 20件 成功（口のテスト18、見積もり2）、`tsc` の型検査 成功
- PC側 186件 成功（通し5件：ローカルの中継をNodeで起動）

無料枠の見積もり（偽のRedisで数えた値）
| 場面 | コマンド | 帯域 | 仕様書の見込み |
| --- | --- | --- | --- |
| 大会4日（14時間/日、アプリ3時間/日） | 約10.4万（20.8%） | 約343MB（3.4%） | 約29%・約4% |
| 30日つけっぱなし | 約25.9万（51.8%） | 約12MB（0.1%） | 約52% |

完了条件
- 往復（手動スキャン・帯域ON/OFF・記録の閲覧・ラベル・歩測）：`test_round_trip_all_operations`
- コマンド数の上限をテストで固定：`relay/test/api.test.ts`
- トークンなし・違うトークン・役割違いは断られ、Redisのコマンドは0
- 60秒を過ぎた指示はPCに渡らない（中継・PCの両方）、同じ要求IDで結果が変わらない、受け取って保存前に落ちても再起動後にもう一度届く
- 中継が止まっても監視と通知（偽のntfy）は止まらず、戻ったら溜めた記録が送られる

デプロイはまだしていない（`docs/relay.md` の手順でユーザーが行う）。

## 次にやること

M7 実機の層：radiod（特権の小さな補助プロセス）と RemoteRadio、iw・ipの操作、合否ゲート、復旧、survey、`deploy/`、`docs/HARDWARE_TEST.md`。
