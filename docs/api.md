# API（確定版）

PCのローカルAPI（この文書）と、中継のAPI（`docs/relay.md`）がある。時刻はすべてUTCのエポック秒（小数）、chは周波数（MHz）。

## PCのローカルAPI

- `127.0.0.1:8080`（`config.json` の `api.port`）にだけbindする。デスクトップの画面専用で、外からの接続は受けない。
- Hostヘッダが `127.0.0.1` か `localhost`（とポート）でなければ 403 `bad_host`。
- 書き込み（POST/PUT/PATCH/DELETE）は `X-Wifiwatch: 1` ヘッダが要る（無ければ 403 `missing_header`）。Originヘッダがあれば自分（`http://127.0.0.1:ポート` か `http://localhost:ポート`）でなければ 403 `bad_origin`。CORSは許さない（OPTIONSは403）。
- 誤りは `{"error": "コード", "message": "日本語の説明"}`。
- `/docs` などAPIの自動の説明ページは出さない。

### 状態

| メソッドとパス | 中身 |
| --- | --- |
| `GET /api/state` | 画面に要るものを1回で返す（下の表） |
| `GET /api/stations` | 局の一覧（いる局と、最近までいた局） |
| `GET /api/stations/{bssid}` | 局の詳細（機種推定の手がかり、RNR、関係する警報、つながっている端末） |
| `GET /api/clients` | 端末の一覧（データフレームのMACヘッダだけから作ったもの） |
| `GET /api/events?limit=&since_id=&category=` | イベント（新しい順）。category：alert / scan / bands / mode / own / tag / label / walk / radio / calibration / system |

`GET /api/state` の主な中身：

| 鍵 | 中身 |
| --- | --- |
| `mode`・`mode_elapsed_s`・`pre_rounds` | モード、切り替えてからの秒数、PREの終わった周の数 |
| `own` | 自チーム名、自ch（`freq`・`label`・`width`）、受信しているブロック（`block`）、予備ch、自chの受信率（`ratio`）、活動量とリトライ率の10秒ごとの推移（`series`）と最新値 |
| `rx` | 受信の見張り（`state`：ok / suspect / recovering / failed、`reason`、最後に受信した時刻） |
| `alerts`・`alert_counts` | 発生中の警報（レベルの高い順）と件数 |
| `manual` | 手動スキャンの状態（`state`：queued / running / done、`done`/`total`、残り秒数） |
| `channels` | 57chそれぞれ：`state`（own / in_block / patrol / excluded / scanning / scan_target / paused / stopped）、最後に受信してからの秒数、再訪の間隔、重なる局の数・強い局の数・最大の強さ、活動量、予約しているチーム、問題chか |
| `stations` | 局：主ch、幅、占有範囲（`spans`）、強さ、種類（own / team / official_infra / broadcast / unknown / known_ok）、チーム、機種推定、いるか、関係する警報、新規参入か |
| `suggestions` | 回避先の候補（上位3つ。ピットから見た参考値） |
| `calibration` | PREの下見のまとめ（あれば） |
| `notify`・`relay` | 通知と中継の状態 |

「検知なし」と「見ていない」の区別は `channels[].state` と `observed_ago` で付く。`excluded`（巡回外）、`paused`・`scan_target`（手動スキャン中で今は見ていない）、`stopped`（受信停止中）、`observed_ago` が `null`（まだ一度も見ていない）は「見ていない」。

### 手動スキャン

| メソッドとパス | 中身 |
| --- | --- |
| `POST /api/scan` | 本文 `{"bands": ["6","5","2.4"]?, "freqs": [MHz]?, "confirm": bool?, "request_id": str?}`。どちらも無ければ巡回に入っている全ch。→ `{"id","total","seconds","state"}`。実行中なら 409 `busy`、QUIET中で `confirm` が無ければ 409 `needs_confirm` |
| `GET /api/scan/{id}` | 進み具合。終われば `state: done` と記録のID（`record`） |

### 巡回の帯域ON/OFF

| メソッドとパス | 中身 |
| --- | --- |
| `GET /api/bands` | 帯域とchごとの「外しているか」、自ブロックのch（外せない）、枠の割り当て |
| `PUT /api/bands` | 本文 `{"excluded_bands": ["2.4"], "excluded_freqs": [MHz], "request_id"?}`（全体を置き換える）。自ブロックのchを外す要求は 400。再起動しても残る |

### 記録とラベル

| メソッドとパス | 中身 |
| --- | --- |
| `GET /api/records?kind=&since=&until=&location=&band=&tag=&limit=` | 記録の一覧（新しい順）。kind：pc_scan / walk |
| `GET /api/records/{id}` | 記録の本文とラベル |
| `PATCH /api/records/{id}` | ラベルの変更 `{"location": str|null, "tags": [str], "memo": str}`。場所40文字・タグ10個×24文字・メモ1000文字まで。編集時刻の新しい方を残す（`applied` が false なら、もっと新しい編集がある） |
| `GET /api/records/export?format=json|csv&…` | 書き出し（絞り込みは一覧と同じ）。CSVは局ごとに1行、BOM付きUTF-8 |

### デスクトップだけの操作（中継からは受けない）

| メソッドとパス | 中身 |
| --- | --- |
| `POST /api/mode` | `{"mode": "PRE"|"LIVE"|"QUIET"}` |
| `POST /api/own` | `{"channel": MHz または "6g37", "width": 20|40|80|160|320}`（再起動なしで自chを変える） |
| `POST /api/tags` | `{"bssid": "…", "team": "名前" または null}`（手動のチーム名タグ。以後は割当違反の判定に回る） |

## 中継のAPI

`docs/relay.md` を参照。
