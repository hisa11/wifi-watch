# 決めたこと・置いた仮定・確かめたドキュメント

仕様書で「暫定」「未決」「未確認」の点と、依頼書に無い実装上の選択を1行ずつ残す。形式：**何を** — なぜ — どう仮定したか。

## 環境・技術の選択

- **開発PC** — Ubuntu 24.04.5、カーネル7.0、Python 3.12.3（pyenv）、Node 22.21、npm 10.9、JDK 21、Android SDK（platforms 33〜36.1、build-tools 36/37、emulatorあり、system-imagesとcmdline-toolsなし）、`iw` なし。無線はRTL8852BE（AX210ではない）— 実機の層はここでは動かさない。（2026-10-14確認）
- **PC側のPython** — Ubuntu標準3.10以上で動く書き方にする（`from __future__ import annotations`、3.11以降の機能は使わない）。開発は3.12。依存は `server/requirements.txt` で版を固定。
- **scapyを使わない** — 依頼書は「足りなければscapyをやめる」だが、毎秒5000フレームを1コアで処理する目安とペイロードを読まない方針から、最初から軽い自前の解析にした（`server/wifiwatch/capture/`）。処理能力の測定はM1で `PROGRESS.md` に残す。
- **TypeScriptは5.9.3** — npmの最新は7.0.2（Go製の新実装）だが、Vercelの関数のビルドがTypeScriptのJS APIを使う可能性があり、7系の互換が未確認のため。テストはvitest 5.0.4。
- **中継は1つの関数** — `api/index.ts` に `/api/*` をまとめ（`vercel.json` の rewrites）、中身は `src/` のルータで振り分ける。冷えた起動の回数を減らし、テストもしやすいため。
- **中継のNode.jsは22.x** — 開発PCと同じ版にする（Vercelは24.xが既定、22.xも可）。
- **中継に認証なしの口を作らない** — 死活確認用の口も含め、すべてトークン必須（依頼書3章）。トークンは16文字未満なら誰も通さない。

## 確かめた外部ドキュメント

- **2026-10-14 Vercel Functions（Node.js）** — `api/` に `export default { fetch(request) }` を置けばWeb標準のRequest/Responseで書ける。Node.jsは24.x（既定）と22.x。`package.json` の `engines.node` で指定。（vercel.com/docs/functions/runtimes/node-js、…/node-js-versions）
- **2026-10-14 Vercelの上限** — 要求・応答の本文は最大4.5MB（超えると413）。Hobbyの最大実行時間は300秒。メモリはHobby 2GB。関数は既定でiad1の1リージョン。Hobbyでも1リージョンなら任意に選べる（`regions`）。（vercel.com/docs/functions/limitations、…/configuring-functions/region）
- **2026-10-14 Upstash Redisの無料プラン** — 月50万コマンド、データ250MB、帯域10GB/月、1要求10MB、1レコード100MB、DBは1つ。AUTH・PINGなどの運用コマンドは数えない。パイプライン・トランザクション・EVALの数え方は書かれていない → **使わない**。1コマンドずつ数える前提で見積もる。東京リージョンが無料で選べるかは書かれていない（未確認のまま）。（upstash.com/pricing/redis、upstash.com/docs/redis/overall/pricing）
- **2026-10-14 MarketplaceでつないだときのUpstashの環境変数名** — ドキュメントで確認できなかった。`UPSTASH_REDIS_REST_URL`/`_TOKEN` と `KV_REST_API_URL`/`_TOKEN` の両方を読む作りにした。実際につないだときに `docs/relay.md` の手順で確かめる。
- **2026-10-14 @upstash/redis 1.39.0** — `automaticDeserialization: false` で値を文字列のまま扱う。

## 無線・解析

- **chの書き方** — 設定では `"6g37"`・`"5g36"`・`"2g1"`・`"6GHz ch37"`・MHzの整数を受け付け、内部はすべてMHz。日本で使えないch（6GHzの偶数ch、5GHzのch149以上など）は読み込みで止める。
- **日本の5GHz** — W52/W53/W56の20ch（ch144を含む）。束ねられるのは40MHz（10本）、80MHz（36・52・100・116・132）、160MHz（36・100）。ch132〜144は80MHzまで。
- **自ブロック** — 自分のchを含む160MHzブロック。無ければ80→40→20MHz（予備chや自分のchが5GHzのch132〜144のとき）。
- **320MHzが2本候補になるch（ch33〜61）** — 局の範囲はEHT OperationのCCFS1で決める。束ね方の表から選ぶときは低い方（中心ch31）。
- **強さの合成** — アンテナごとの値（radiotapの2つ目以降の名前空間）があれば電力の和で合成し、無ければドライバが最初の名前空間に入れた値を使う。推測：iwlwifiの最初の値は2本の強い方で、和ではない（未確認）。
- **主chと幅の優先** — 幅はEHT Operation（情報あり）→ HE Operationの6GHz情報 → VHT Operation → HT Operation。6GHzのHE 6GHz情報で160MHzかつCCFS1=0のときは、CCFS0が160MHzブロックの中心なら160MHz、そうでなければ80MHzとみなす。IEが主chと矛盾したら20MHzとみなし「疑わしい」印を付ける。
- **Multiple BSSID（IE 71）の非送信BSS** — 今は展開しない（送信BSSだけ局の表に載る）。6GHzで1台が複数SSIDを出す場合、2つ目以降のSSIDは見えない。必要になったら追加する（提案）。

## 偽の無線のモデル（実機で確かめるまでの仮定）

- 受信機は主20MHzで検出する。20MHzのビーコンは局の主chでだけ聞こえる。重複ビーコンの局は局の帯域内のどの20MHzでも聞こえる。
- データは局の全幅で送る。受信機の主20MHzが局の範囲内で、局の範囲が受信の幅に収まるときだけ聞こえる（160MHz受信で他局の160MHz送信が受信できる、は未検証の仮定）。320MHzのデータは聞こえない。
- ACKは局の帯域に重複して送る。
- 切替に30ms、感度-91dBm。

## 設定

- **再起動後も残す設定** — 帯域ON/OFF、モード、自分のchの変更は `data_dir/overrides.json` に原子的に書く。壊れていたら既定に戻して監視を続け、壊れたファイルは `overrides.broken.json` に残す。
- **ntfyのトピック** — 公開トピックなので16文字以上の英数字を必須にした（仕様書「推測されにくい文字列」）。
- **ローカルAPIのbind先** — `api.host` は `127.0.0.1` 以外を設定の検証で拒む。
