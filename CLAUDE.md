# wifiwatch — 作業の約束（Claude Code 向け）

NHK高専ロボコン全国大会向けのWi-Fi監視システム。部品は3つ：ピットのPC側（`server/`、Python）、自作のAndroidアプリ（`android/`、Kotlin）、その2つをつなぐ中継（`relay/`、Vercel＋Upstash Redis、TypeScript）。

## 続きから始めるとき

1. `docs/PROGRESS.md` の最後の節（どこまで終わったか・次にやること）を読む。
2. 振る舞いの正は `docs/spec.md`（仕様書）。実装の方針は `docs/brief.md`（依頼書）。食い違ったら仕様書を優先。
3. 仮定と確かめた外部ドキュメントは `docs/DECISIONS.md`、作業単位の計画は `docs/PLAN.md`。
4. マイルストーンが終わるたびに `docs/PROGRESS.md` に追記し、gitにコミットして、日本語で短く報告する（やったこと／テスト結果／仕様書との差／置いた仮定／次）。pushは言われるまでしない。

## テストの動かし方

```bash
cd server && .venv/bin/python -m pytest -q          # PC側
cd relay && npm test                                 # 中継
cd android && ./gradlew testDebugUnitTest            # アプリ（JVMの単体テスト）
server/.venv/bin/python tools/dev_stack.py           # ローカルの中継＋偽の無線のPC（エミュレータからの通し。--part relay|pc で別々に）
./start.sh [scan|monitor|fake]                       # このPCで起動（scan・monitor は sudo が要るのでユーザーが実行する）
```

開発用PCの無線はRealtek RTL8852BE（AX210ではない）。実機の層（iw・ip・AF_PACKET）はここでは動かさず、偽の無線（`server/wifiwatch/radio/fake.py`）で開発する。実機のコマンドはユーザーが実行する（`docs/HARDWARE_TEST.md`）。

## 守ること

- 受信専用。AX210から送信・パケット注入・deauthをしない。管理モードに戻すのは、合否ゲートと復旧のときのスキャンだけ。
- データフレームはMACヘッダまでしか見ない。ペイロードは読まない・保存しない。pcapを保存しない。
- chは周波数（MHz）で持つ。ch番号は帯域をまたいで衝突する（2.4GHzのch1は2412MHz、6GHzのch1は5955MHz）。
- 6GHzの局の主chは、受信した周波数ではなく、HE Operationに書かれた主chで決める（重複ビーコンは主ch以外でも届く）。
- 活動量やフレーム数は「そのchを実際に受信していた秒数」で割る。時計の秒で割らない。
- 「検知なし」と「見ていない」（巡回外、手動スキャン中、受信停止中）を、データでも、デスクトップの画面でも、アプリでも区別する。
- PCは外からの接続を受けない。PCのAPIと画面は `127.0.0.1` にだけbindする（`0.0.0.0`、テザリングのIFにはbindしない）。ポートを開けない。VPNやトンネルを入れない。遠隔の操作は、PCから中継への外向きの問い合わせだけで行う。
- ローカルAPIは、Hostヘッダが `127.0.0.1` か `localhost` の要求だけ受ける。書き込みは独自ヘッダ（例：`X-Wifiwatch: 1`）付きの要求だけ受ける。ブラウザを経由したなりすまし（DNS rebinding、CSRF）を防ぐため。
- 中継のAPIは、読み出しも含めてトークン必須。トークンはアプリ用とPC用に分け、アプリ用のトークンではPC側のAPIを使えない。トークンの検査はRedisに触る前に行う（無料枠を他人に使わせない）。
- 中継が受ける指示は、手動スキャンと帯域ON/OFFだけ。モードの切替、設定の書き換え、任意のコマンドは受けない。値は中継とPCの両方で検査する。
- 指示の期限は60秒。中継は期限を過ぎた指示をPCに渡さない。PCも、受け取った時点で期限を過ぎていれば実行しない。
- 中継はWebSocketや常時接続を使わない。1回の問い合わせで使うRedisのコマンド数は、4.2の上限を超えない（無料枠を守るため）。
- 通知（ntfy・Discord）は中継を通さない。中継が止まっても、PCの監視と通知は止めない。
- subprocessに渡すのは、検証した周波数・幅・IF名だけ（`shell=True` は使わない）。
- 時刻はUTCで保存し、表示はJST。
- sudoが要るコマンドと、NICの状態を変えるコマンド（`ip link set ... down`、`iw ... set type monitor` など）は、実行する前に必ず私に確認する。作業中のPCのWi-Fiが切れるため。
- 秘密情報（アプリ用とPC用のトークン、Upstashの接続情報、ntfyのトピック、Discord Webhook、死活監視のURL）はコミットしない。中継の秘密はVercelの環境変数に置く。コミットするのは `config.example.json` と `relay/.env.example` だけ。

### 補足（依頼書の1章・7章から）

- 先にユーザーに聞くのは2つだけ：不可逆な操作と外に出る操作（削除、force push、sudo、ネットワークや権限の設定変更、Vercelへのデプロイ、外部サービスのアカウント・課金・環境変数の操作）と、読み違えると全部やり直しになる分岐。それ以外は仮定を置いて進め、`docs/DECISIONS.md` に1行ずつ残す。
- Vercel・Upstash・Androidの制限とAPIは、使う前に現行のドキュメントで確かめ、確かめた日付と中身を `docs/DECISIONS.md` に残す。
- やらないこと：送信を伴う機能、pcapの保存、通信の中身の解析、iOSアプリ、PCへの外からの接続、WebSocketや常時接続、中継でのモード切替や任意のコマンドの受け付け、仕様書にない機能の追加（提案は `DECISIONS.md` に書くだけ）。
- 自分で `sudo apt` はしない。足りないものは入れ方をユーザーに示す。
