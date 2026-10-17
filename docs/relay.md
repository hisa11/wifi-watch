# 中継（Vercel Functions ＋ Upstash Redis）

アプリもPCも外向きに中継へつなぐ。PCは外からの接続を受けない。WebSocketや常時接続は使わない。

```
アプリ ──モバイル回線──▶ 中継（Vercel＋Upstash） ◀──テザリング回線（PCから問い合わせ）── PC
                                                       PC ──▶ ntfy・Discord（中継を通さない）
```

## 守ること

- 読み出しも含め、すべての口でトークンが要る。アプリ用（`APP_TOKEN`）とPC用（`PC_TOKEN`）は別。アプリ用のトークンではPC側の口を使えない。トークンの検査はRedisに触る前に行う（違えばRedisのコマンドを1つも使わない）。16文字未満のトークンは設定されていても誰も通さない。
- 中継が受ける指示は、手動スキャン（`scan`）と帯域ON/OFF（`bands`）だけ。モードの切替・設定の書き換え・任意のコマンドは受けない。値（帯域・ch・要求ID・本文の大きさ）は中継とPCの両方で検査する。
- 指示の期限は60秒（中継の時計）。中継は期限を過ぎた指示をPCに渡さず消す。PCも受け取った時点で期限を過ぎていれば実行しない（中継の時計どうしで比べる）。

## Redisの持ち方

| キー | 型 | 中身 |
| --- | --- | --- |
| `ww:ver` | 文字列（整数） | 版数。アプリが受信箱に置いたとき・アプリを開いたときに1増やす。PCは「前回と違うか」で比べる（大小ではない。Redisが空に戻っても取りこぼさない） |
| `ww:fg` | 文字列（期限10秒） | アプリが前面にいる印。アプリの問い合わせのたびに延ばす |
| `ww:inbox` | ハッシュ | 受信箱。PCが「保存した」と返すまで消さない（受け取った直後にPCが落ちても失わない） |
| `ww:state` | 文字列 | PCの状態の写し（8KB以下）と、中継が受け取った時刻。消さずに上書き |
| `ww:rec:<id>` | 文字列 | 記録の写し（本文） |
| `ww:recmeta` | ハッシュ | 記録の一覧用の情報（id → JSON） |
| `ww:recupd` | ソート済み集合 | 更新時刻（中継のms）の索引。アプリは「前回から更新されたもの」だけ取る |
| `ww:recbytes` | 文字列（整数） | 記録の写しの合計バイト数。100MBを超えたら更新の古い順に消す（PCが正本なので消えても困らない） |

受信箱の項目：

| 項目 | 中身 |
| --- | --- |
| `cmd:scan` | 手動スキャン（同時に1つだけ。PCがまだ受け取っていない手動スキャンがあれば次は断る） |
| `cmd:<要求ID>` | 帯域ON/OFF |
| `lbl:<記録ID>:<要求ID>` | ラベルの変更（編集ごとに別の項目。続けて変えても前の変更を上書きで消さない） |
| `walk:<記録ID>` | 歩測の記録（同じ記録は同じ項目なので、二重に届いても1件） |

アプリは「受信箱に置いてから版数を上げる」。逆の順だと、PCが新しい版数で受信箱を読んだときにまだ項目が無く、取りこぼす。

## 1回の問い合わせで使うコマンド数

| 問い合わせ | 上限 | 実際 |
| --- | --- | --- |
| PCの待機中（アプリが前面にいない） | 1 | `MGET ver fg` だけ |
| PCのアプリ前面中 | 4 | `MGET`・状態の写しの `SET`（＋版数が変わったときだけ `HGETALL` と `HDEL`） |
| アプリの2秒ごとの問い合わせ | 2 | 前面の印の `SET EX`・`GET state`（開いた直後だけ `INCR ver` が加わり3） |
| 置く操作（指示・ラベル・歩測） | 5 | 2（`HSETNX`/`HSET`・`INCR`）。重複の確認で `HGET` が加わることがある |
| 記録の写しを置く（PC） | 5 | `HGET`・`SET`・`HSET`・`ZADD`・`INCRBY`（合計が100MBを超えたときだけ、消す分が加わる） |
| 記録の一覧・詳細（アプリ） | 2 | `ZRANGE BYSCORE`・`HMGET` ／ `GET`・`HGET` |

`relay/test/api.test.ts` でこの上限をテストで固定している。パイプライン・トランザクション・Luaは使わない（Upstashの数え方がドキュメントに無いため）。

## 無料枠の見込み（`relay/test/usage.test.ts`、偽のRedisで数えた値）

前提：状態の写しは1回8KB近くに水増し、記録は1件約20KB、手動スキャンは1日30回。

| 場面 | Upstashのコマンド | Upstashの帯域 | 仕様書の見込み |
| --- | --- | --- | --- |
| 大会4日（1日14時間稼働、アプリを開くのは1日3時間） | 約10.4万（月の枠の約20.8%） | 約343MB（約3.4%） | 約29%・約4% |
| 30日つけっぱなし（アプリは開かない） | 約25.9万（約51.8%） | 約12MB（約0.1%） | 約52% |

Vercelの関数の呼び出しは大会1日あたり約1.5万回（PC 9,360＋アプリ 5,400）。

## API（確定版）

すべて `Authorization: Bearer <トークン>`。本文はJSON。誤りは `{"error": "コード", "message": "日本語"}`。時刻：`accepted_at`・`now`・`received_at` は中継の時計（UNIXミリ秒）、`edited_at`・`created_at` はUTCのエポック秒。

### アプリ用（`APP_TOKEN`）

| メソッドとパス | 本文 → 応答 |
| --- | --- |
| `POST /api/app/poll` | `{"opened": true?}` → `{"now", "state", "state_received_at", "state_age_ms"}`。2秒ごと（前面にいる間だけ）。開いた直後は `opened: true` |
| `POST /api/app/commands` | `{"request_id", "type": "scan"|"bands", "params"}` → `{"id", "accepted_at", "now", "expires_at"}`。同じ要求IDは `duplicate: true`。PCが受け取っていない手動スキャンがあれば 409 `busy` |
| `GET /api/app/records?since=<ms>&limit=` | → `{"records": [一覧の情報…], "next_since", "more"}`（`since` より後に更新されたものだけ） |
| `GET /api/app/records/{id}` | → 記録の本文＋`meta`。無ければ404 |
| `PATCH /api/app/records/{id}` | `{"request_id", "label": {"location", "tags", "memo"}, "edited_at"}` → `{"id", "accepted_at"}` |
| `POST /api/app/records` | `{"request_id", "record": {"id", "kind": "walk", "created_at", "stations": […], "label"?…}}`（512KBまで）→ `{"id", "accepted_at"}` |

`params`：`scan` は `{"bands"?: ["6","5","2.4"], "freqs"?: [MHz], "confirm"?: bool}`、`bands` は `{"excluded_bands": […], "excluded_freqs": [MHz]}`（全体を置き換える）。

指示の進み具合は、状態の写しの `commands[要求ID].state`（received → running → done、または rejected・expired）と `manual`（手動スキャンの残り秒数）で分かる。`accepted_at` から60秒（中継の時計）たっても写しに出なければ「PCに届かなかった」。

### PC用（`PC_TOKEN`）

| メソッドとパス | 本文 → 応答 |
| --- | --- |
| `POST /api/pc/sync` | `{"version": str|null, "acks": [受信箱の項目…], "state"?: {…8KB以下}}` → `{"now", "version", "changed", "fg", "inbox"?: [{…, "field"}]}`。`inbox` は版数が変わったとき（または `version: null`）だけ。期限を過ぎた指示は入れずに消す |
| `PUT /api/pc/records/{id}` | `{"record": {…ラベル込みの本文}}`（512KBまで）→ `{"id", "updated_at", "total_bytes", "trimmed"}` |

PCの動き（`server/wifiwatch/relay_client.py`）：起動直後は `version: null`。前面の印があれば2秒、無ければ10秒ごと。保存できた項目だけ次の問い合わせで `acks` に入れる。保存できなかったら版数を忘れて次も全部受け取る。

## ローカルの中継（開発とテスト）

`vercel dev` もVercelへのログインも要らない。本番と同じ `handle` と偽のRedisで、127.0.0.1だけで待つ。

```bash
cd relay && npm run build
APP_TOKEN=app-dev-token-0123456789 PC_TOKEN=pc-dev-token-0123456789 node dist/src/dev-server.js 8787
```

PCの `config.json` に `"relay": {"url": "http://127.0.0.1:8787", "pc_token": "pc-dev-token-0123456789"}`。

## デプロイの手順（ユーザーが行う）

外部サービスのアカウント・課金・環境変数の操作とデプロイはユーザーが行う（または確認を取ってから行う）。

1. **Vercelのプロジェクト**：GitHubなどから取り込むか `vercel` CLIで、ルートディレクトリを `relay/` にしたプロジェクトを作る（フレームワークは「Other」）。プランはHobby。
2. **リージョン**：`relay/vercel.json` の `"regions": ["hnd1"]`（東京）で関数を動かす。Hobbyでも1リージョンなら選べる。
3. **Upstash**：Vercelのダッシュボードの Storage（Marketplace）から Upstash for Redis を作り、このプロジェクトにつなぐ。プランは無料、リージョンは東京（`ap-northeast-1`）を選べれば選ぶ（無料で選べるかは未確認）。
4. **環境変数の名前を確かめる**：つないだあと、`vercel env ls`（または Settings → Environment Variables）で名前を見る。2026-10-17 に実際につないだときは `KV_REST_API_URL`/`KV_REST_API_TOKEN`（ほかに `KV_REST_API_READ_ONLY_TOKEN`・`KV_URL`・`REDIS_URL`）が入った。中継は `UPSTASH_REDIS_REST_URL`/`UPSTASH_REDIS_REST_TOKEN` と `KV_REST_API_URL`/`KV_REST_API_TOKEN` のどちらでも読む。
5. **トークン**：`APP_TOKEN` と `PC_TOKEN` を別々の32文字以上の乱数で作り、Production（と使うならPreview）の環境変数に置く。リポジトリには書かない。
   ```bash
   python3 -c "import secrets; print(secrets.token_urlsafe(32))"
   ```
6. **デプロイ**して、本番のドメイン（例：`https://wifiwatch-relay.vercel.app`）を控える。
7. **Deployment Protection**（2026-10-15確認）：既定の Standard Protection は「本番ドメイン以外」を守る。アプリとPCには**本番ドメイン**を設定する（デプロイごとに作られるURLは保護されて届かない）。「All Deployments」にすると本番ドメインも守られ、アプリもPCも届かなくなるので選ばない。
8. **設定**：PCの `config.json` に `relay.url`（本番ドメイン）と `relay.pc_token`。アプリの設定画面に中継のURLとアプリ用のトークン。
9. **確かめる**：トークンなしで `curl -X POST https://…/api/app/poll` が401になること。アプリを開いて、PCの画面の右上が「中継 アプリ表示中」になること（10秒以内）。

## 確かめたこと（2026-10-15）

- ローカルの中継（Node＋偽のRedis）を挟んで、アプリ役 → 中継 → 偽の無線で動くPC → 中継 → アプリ役の往復（手動スキャン、帯域ON/OFF、記録の閲覧、ラベル、歩測）が通る（`server/tests/test_relay_e2e.py`）。
- 中継を止めて戻しても、監視と通知は止まらず、溜めた記録の写しは戻ったときに送られる。PCが受け取ってから保存するまでに落ちても、再起動後にもう一度届く。
