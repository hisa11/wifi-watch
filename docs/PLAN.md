# 実装計画（作業単位）

依頼書（`docs/brief.md`）6章のマイルストーンを、作業単位に分けたもの。終わった単位には `[x]` を付ける。仮定は `DECISIONS.md`、進み具合は `PROGRESS.md`。

## 全体の形

```
server/wifiwatch/
  clock.py         時刻の注入（実時間／偽の時計）
  channels.py      日本の57ch（MHz）、帯域、PSC、40/80/160/320MHzのブロック、重なり
  config.py        config.json の検証、画面とアプリから変えた設定の保存
  capture/         radiotap・802.11ヘッダ・IEの軽い自前解析（scapyは使わない）
  radio/           無線の共通インタフェース、偽の無線、実機（iw・ip・AF_PACKET）、特権の小さな補助プロセス（radiod）
  scheduler.py     PRE/LIVE/QUIET、巡回の枠、手動スキャン、帯域ON/OFF、生存確認
  analyzer/        局の表、chごとの統計、警報
  classify.py      署名、チーム対応表、OUI、機種推定、RNRの紐付け
  notify.py        ntfy・Discord、抑制、無期限の再送、稼働中通知、死活監視、警報音
  store.py         SQLite、events.jsonl、channels.csv、書き出し、中継向けの待ち行列
  controller.py    ローカルAPIと中継の指示が共通で呼ぶ内部の関数
  api.py + static/ ローカルAPI（127.0.0.1だけ）とダッシュボード
  relay_client.py  中継への問い合わせ（別スレッド）
  engine.py        部品の組み立てと受信ループ
relay/  src/（Redisを引数に取る中身）、api/（Vercelの薄い層）、dev-server（ローカルの中継）
android/ Kotlin + Compose + Room + WorkManager
deploy/ 合否ゲート、セットアップ、pm2の設定
testdata/ 合成フレーム・IE・中継とのやりとりの見本（3部品で共用）
```

## M0 土台

- [ ] git、`CLAUDE.md`、`docs/`（spec、brief、PLAN、DECISIONS、PROGRESS）、`.gitignore`
- [ ] `server/`：venv、依存の版の固定、pytestの設定
- [ ] `channels.py`：57chの表（2.4GHz 13、5GHz 20、6GHz 24）、PSC、ブロック、重なりの計算
- [ ] `clock.py`：実時間と偽の時計
- [ ] `config.py`：`config.example.json` とその検証（間違いは分かる言葉で止める）
- [ ] `capture/build.py`：合成フレームの生成器（radiotap、ビーコン、HE/EHT Operation、重複ビーコン、RNR、deauth、リトライ付きデータ、FCS付き）
- [ ] `radio/base.py`・`radio/fake.py`：無線の共通インタフェースと偽の無線（シナリオの局が合成フレームを出す）
- [ ] `tools/gen_testdata.py` → `testdata/`
- [ ] `relay/` の雛形：TypeScript、ビルドとテストが通る、偽のRedis（コマンド数と転送量を数える）

## M1 解析

- [ ] radiotap（拡張present、名前空間、ベンダ名前空間、整列、合成RSSI、FCSフラグ、PPDUの帯域）
- [ ] 802.11ヘッダ（種類、アドレス、Retry、Protected、QoS、HTC）
- [ ] IE（SSID、DS、HT/VHT Operation、HE Operation＋6GHz情報、EHT Operation、RNR、RSN、WPS、ベンダIE、国、HE/EHT Capabilities）
- [ ] 主chと帯域幅の決定（EHT → HE 6GHz → VHT → HT の順）
- [ ] 局の表（主chで1件、重複ビーコンは主chで数える、受信したchは別に記録）、端末の表（MACヘッダだけ）
- [ ] chごとの統計（受信秒数で割る、(ch, 受信幅) ごと、10秒の桶）
- [ ] AF_PACKETのBPFで、データフレームをカーネル内でMACヘッダまでに切る（ペイロードがユーザ空間に来ない）
- [ ] 処理能力の測定（毎秒5000フレーム／1コア）
- 完了条件：6GHzのビーコンが局の表に載り、主chと帯域幅（160/320MHzを含む）が正しい。重複ビーコンを主chで数える。

## M2 巡回

- [ ] 枠の割り当て（LIVE：自ブロック7秒＋12枠、自ブロック内4・6GHz他4・5GHz3・2.4GHz1、予備chが5GHzなら専用1枠）
- [ ] PRE（57chを250msずつ）、QUIET（自ブロック内1秒/10秒、30秒ごとに5GHz 250ms）
- [ ] 手動スキャン（1つだけ、QUIETでは確認、終わったら元のモード、結果は記録）
- [ ] 帯域ON/OFF（枠の配り直し、自ブロック内の4枠と生存確認は残す、自ブロックは外せない、再起動後も残る、イベントに残す）
- [ ] 初回起動の先回り（自ブロックとPSCを短い滞在で1周）
- [ ] 切替間隔200ms以上の保証
- 完了条件：仕様書の表と照合（受信率、再訪の間隔の最大、PREの1周、手動スキャンの所要時間、配り直し）

## M3 警報

- [ ] 警報の枠組み（鍵、当事者、発生・追加・解消、10秒で解消）
- [ ] 表の全警報（割当違反2種、新規参入×重なり、正体不明の強力局・広帯域局、RoboconFree・CineEyeの6GHz出現、活動量、リトライ率、同名SSID、deauth洪水、受信停止）
- [ ] 生存確認と復旧（モニタ再設定 → ドライバ読み込み直し → 管理モードでスキャン → JPと6GHzの確認 → モニタ → 通知）
- [ ] 分類（署名、チーム対応表、手動タグ、OUI、機種推定、RNR）
- 完了条件：警報ごとに鳴るべき場面と鳴ってはいけない場面のテスト。静かな6GHzで受信停止が鳴らない。PRE→LIVEで活動量が鳴らない。

## M4 通知

- [ ] ntfy・Discord、優先度、critは両方
- [ ] 抑制（30秒）、追加、解消
- [ ] 再起動しても消えない待ち行列と無期限の再送
- [ ] 稼働中通知（起動時と15分ごと）、死活監視（1分ごと）、警報音（USBシリアルに1バイト）
- 完了条件：偽のHTTPサーバで、回線が切れても警報が失われず、戻ったら届く。

## M5 保存・ローカルAPI・画面

- [ ] SQLite（記録、ラベル、イベント、設定、待ち行列）、events.jsonl、channels.csv、JSON/CSVの書き出し
- [ ] controller（API・中継の共通の実行経路）
- [ ] ローカルAPI（Hostヘッダ検査、書き込みの独自ヘッダ検査、127.0.0.1だけ）
- [ ] ダッシュボード（アナライザーのグラフ1秒更新、再読込で即表示、警報、局・端末、イベント、記録とラベル、手動スキャン、帯域ON/OFF、巡回外の灰色表示、モード、自chの変更、チーム名のタグ付け、回避先の提案、問題chの強調）
- 完了条件：偽の無線で起動し、ブラウザから全部の操作ができる。Host・独自ヘッダの検査を、断る場面と通す場面の両方でテスト。

## M6 中継

- [ ] Vercel・Upstashの現行ドキュメントを確かめる（関数の書き方、リージョン、環境変数、コマンドの数え方、上限）
- [ ] `relay/src`：トークン検査（Redisの前）、アプリ用・PC用のAPI、受信箱、版数、前面の印、状態の写し、記録の写し、期限60秒、冪等
- [ ] `relay/api`：Vercelの薄い層、`vercel.json`（hnd1）
- [ ] ローカルの中継（偽のRedis）
- [ ] `relay_client.py`：問い合わせ間隔、溜めて再送、記録とラベルの同期、指示の実行（controller経由）、状態の写し
- [ ] テスト：往復、コマンド数の上限、月の使用量の見積もり、トークン、期限、冪等、落ちても失わない、中継が止まっても監視と通知は続く
- [ ] `docs/relay.md`、`docs/api.md`

## M7 実機の層

- [ ] radiod（特権の小さな補助プロセス）と、その相手（RemoteRadio）
- [ ] iw・ipの操作（検証した値だけ）、合否ゲート、復旧、survey dump、fcsfail
- [ ] `deploy/`：合否ゲートのスクリプト、セットアップ、NetworkManagerの管理外、pm2の設定
- [ ] `docs/HARDWARE_TEST.md`

## M8 アプリ

- [ ] Androidの現行ドキュメントを確かめる（Wi-Fiスキャンの権限と制限、ScanResultのIE、requestNetwork、WorkManager）
- [ ] Gradleの雛形、Compose、Room、WorkManager、minSdk 30
- [ ] モバイル回線への固定（ソケットと名前解決）
- [ ] 6画面（状態、手動スキャン、帯域ON/OFF、記録、歩測、設定）
- [ ] IE解析を `testdata/` でPC側と照合
- [ ] 溜めと再送（要求ID）
- [ ] 実機での確かめ方を `HARDWARE_TEST.md` に

## M9 通し

- [ ] PC（偽の無線）・中継（ローカル）・アプリ（エミュレータ）で通す
- [ ] 中継を止めて戻す、PCを再起動する、で何も失わない
