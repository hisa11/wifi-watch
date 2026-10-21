# wifiwatch

NHK高専ロボコン全国大会のピットで、会場の Wi-Fi（6GHz帯が中心）を受信だけで見張り、他チームとの干渉やその兆候を部員のスマホに知らせるシステム。

| 部品 | 場所 | 中身 |
| --- | --- | --- |
| PC側 | `server/` | Python。ミニPC＋Intel AX210 で受信・解析・警報・画面（`http://127.0.0.1:8080`）・通知 |
| 中継 | `relay/` | Vercel Functions＋Upstash Redis（TypeScript）。アプリとPCが外向きにつなぐ |
| アプリ | `android/` | Kotlin。状態・PCへの手動スキャン・帯域ON/OFF・記録・スマホのアナライザー・警報のプッシュ通知 |

守ること（受信専用、PCは外からの接続を受けない、秘密はコミットしない など）は `CLAUDE.md`、振る舞いは `docs/spec.md`。

## 本番のミニPCのセットアップ

前提：Ubuntu などの Linux、Python 3.10 以上、Intel AX210。AX210 は監視専用になるので、インターネット（中継・通知）は
スマホのUSBテザリングか有線LANで取る。

### 1. 取ってくる・足りないものを入れる

```bash
git clone https://github.com/hisa11/wifi-watch.git ~/wifi-watch
```

```bash
sudo apt install iw ieee-data python3-venv nodejs npm
```

```bash
sudo npm install -g pm2
```

### 2. 無線のIF名を確かめる

AX210 の名前（例：`wlp2s0`）を見る。以下の `wlp2s0` は読み替える。

```bash
ip -br link
```

### 3. セットアップの道具（2回動かす）

```bash
cd ~/wifi-watch && deploy/setup.sh wlp2s0
```

1回目は `config.json` を作って止まる。次の「4」を書いてから、もう一度同じコマンドを動かす。2回目は、sudo が要る段を1つずつ確かめながら進む：
Python の仮想環境と依存 → グループ（`wifiwatch`・`dialout`。反映には一度ログアウト）→ AX210 を NetworkManager の管理から外す
（このPCの Wi-Fi は使えなくなる）→ 合否ゲート（6GHz でモニタモードにできるか。不合格ならスキャンだけのモードで動かす）→ pm2 で常駐。

### 4. config.json を書く（git には入らない）

| 項目 | 書くこと |
| --- | --- |
| `interface` | 手順2の名前 |
| `radio.kind` | `"radiod"` |
| `device_name` | 例 `"pit-pc"` |
| `own` | 自チームの名前・ch（例 `"6g37"`）・幅・予備ch。自チームのAP（`bssids`・`ssids`）は、あとから画面の「設定」で登録してもよい |
| `teams` | 各チームの予約表（名前・ch・予備ch・分かればSSIDの形） |
| `locations` | 場所ラベルの選択肢 |

ch は帯域を付けて書く（`2g1`・`5g36`・`6g37`）。書き方の見本は `config.example.json`。

秘密（トークン・鍵）は手で書かず、道具で入れる（画面に出さない・権限600）：

- 中継（Vercel）のURLとPC用のトークン（PC_TOKEN）
  ```bash
  cd ~/wifi-watch && server/.venv/bin/python tools/set_relay.py config.json
  ```
- アプリへの警報のプッシュ通知（FCM）：Firebase のコンソールで、このPC用に「新しい秘密鍵を生成」して取り込む（運ぶのはUSBメモリなどで。git・チャットに載せない）。詳しくは `docs/push.md`
  ```bash
  cd ~/wifi-watch && server/.venv/bin/python tools/set_notify.py config.json --fcm-key ~/ダウンロード/鍵のファイル名.json
  ```
- アプリを入れない部員（iPhone など）にも知らせるなら ntfy（トピックを作って入れる。スマホに ntfy のアプリを入れて登録）
  ```bash
  cd ~/wifi-watch && server/.venv/bin/python tools/set_notify.py config.json
  ```

### 5. 動かす・止める

`setup.sh` の最後で起動して保存する（`sudo pm2 save`）。PCを再起動しても立ち上がるように、1回だけ：

```bash
sudo pm2 startup systemd
```

様子を見る・ログ・起動し直し：

```bash
sudo pm2 status
```

```bash
sudo pm2 logs wifiwatch
```

```bash
sudo pm2 restart all
```

画面はミニPCのブラウザで `http://127.0.0.1:8080`（このPCの中からだけ見られる）。

### 6. 確かめる

- 試しにアプリへ1件通知を送る（スマホが音なしバイブ／音とバイブで知らせれば完了）
  ```bash
  cd ~/wifi-watch && server/.venv/bin/python tools/set_notify.py config.json --fcm-test
  ```
- 実機での確認の手順と合否の基準：`docs/HARDWARE_TEST.md`（合否ゲート、6GHzの受信、160MHz、アンテナ、テザリング、アプリ など）

**開発PCと同時に動かさない**：同じ PC_TOKEN で2台が中継につながると、アプリからの指示を別のPCが受け取ったり、同じ警報が2台から届いたりする。本番PCで試す間は、開発PCの `./start.sh` を止める。

## スマホのアプリ

1. Firebase の設定ファイル `google-services.json` を `android/app/` に置く（`docs/push.md`。コミットしない）。
2. Android Studio で `android/` フォルダを開いて ▶（Run）、またはスマホをUSBでつないで：
   ```bash
   cd ~/wifi-watch/android && ./gradlew installDebug
   ```
3. アプリの「設定」に、中継のURL（例 `https://wifiwatch-relay.vercel.app`）とアプリ用のトークン（APP_TOKEN）を入れて「つないでみる」。
   通知の許可を「許可」にし、「警報の通知（プッシュ）」が「受け取り中」になっていればよい。
   おやすみモード中も知らせたいときは「緊急・警告はアラームとして知らせる（音なし）」をオン（着信音は消音でなくバイブにしておく）。

## 中継（Vercel）

デプロイ・環境変数・無料枠の見込みは `docs/relay.md`。`relay/` で `vercel --prod`。

## 開発

```bash
cd server && .venv/bin/python -m pytest -q          # PC側
```

```bash
cd relay && npm test                                 # 中継
```

```bash
cd android && ./gradlew testDebugUnitTest            # アプリ（JVMの単体テスト）
```

```bash
./start.sh fake                                      # 偽の無線で画面を動かす（scan・monitor は開発PCの本物の無線。sudo が要る）
```

開発PCでの本物の無線の使い方は `docs/HARDWARE_TEST.md` の「0'」。進み具合は `docs/PROGRESS.md`、決めたことと確かめた外部ドキュメントは `docs/DECISIONS.md`。
