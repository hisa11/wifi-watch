# 実機で確かめること（手順と合否の基準）

実機のコマンドはユーザーが実行する。どれも受信専用で、送信・注入はしない。フレームそのもの（pcap）は保存しない（`hwprobe` は数えて要約を出すだけ）。
結果は下の「記録」の表に書き込み、仮定と違ったら `docs/DECISIONS.md` を直す。

## 0. 準備（ミニPC）

```bash
sudo apt install iw ieee-data python3-venv nodejs npm
sudo npm install -g pm2
git clone <このリポジトリ> ~/wifi-watch
cd ~/wifi-watch && deploy/setup.sh wlan0      # IF名は ip -br link で確かめる（例：wlp2s0）
```

`setup.sh` は sudo が要る段（グループ、NetworkManagerの管理外、合否ゲート、pm2）を1つずつ確認してから実行する。

`hwprobe` を使う間は、本体の受信と取り合わないよう止めておく：`sudo pm2 stop wifiwatch-radiod wifiwatch`（終わったら `sudo pm2 start all`）。
`hwprobe` はモニタモードのIFに対して使う（合否ゲートのあとはモニタモードのまま）。

## 1. 合否ゲート：AX210を6GHzでモニタモードにできるか

```bash
sudo deploy/gate.sh wlan0
```

- 合格：6GHzの24ch（5955〜6415MHz）がすべて disabled なしで並び、モニタモードで `iw dev wlan0 set freq 5955` が通る。終了コード0。
- 不合格なら：radiod を `--scan-only` で動かす（`sudo WIFIWATCH_USER=$USER WIFIWATCH_SCAN_ONLY=1 pm2 start deploy/ecosystem.config.js`）。画面の上に「見ていない：活動量・リトライ率・deauth・160MHz受信（スキャンだけのモード）」と出る。
- あわせて記録：160MHz（`set freq 5955 160 6025`）に合わせられたか。

## 2. 6GHzでフレームが受かるか（20MHz）

```bash
cd ~/wifi-watch/server
sudo .venv/bin/python -m wifiwatch.hwprobe --interface wlan0 frames --ch 6g37 --width 20 --seconds 10
```

- 合格：近くの6GHzのAP（自チームのルータなど）のビーコンが局として出て、主chと幅が正しい（ルータの設定と照合）。
- 記録：「強さの例（ドライバの値, アンテナごと, 合成）」。ドライバの値がアンテナごとの強い方か・和かを見る（DECISIONS「強さの合成」の推測を確かめる）。

## 3. 160MHz幅のモニタで、他局の160MHz送信を受信できるか（仕様書：未検証）

用意：自チームのルータを160MHz（例：ch33〜61、主ch37）にし、スマホをつないで大きなダウンロードを流す。

```bash
sudo .venv/bin/python -m wifiwatch.hwprobe --interface wlan0 frames --ch 6g37 --width 160 --seconds 15
```

- 合格：そのBSSのデータフレームが数えられ、「PPDUの帯域」に160が出る。
- 不合格なら：自ブロックの160MHz受信では広帯域の送信を拾えない。DECISIONS（偽の無線のモデル）を直し、活動量とリトライ率の意味を見直す。

## 4. ビーコンからEHT Operation（320MHzの幅）を読めるか（仕様書：未確認）

用意：Wi-Fi 7ルータを320MHz（6GHz）にする。

```bash
sudo .venv/bin/python -m wifiwatch.hwprobe --interface wlan0 eht --seconds 30
```

- 合格：そのルータが「幅320（eht）」で出る。重複ビーコンを出している場合は「重複ビーコン=True」で、主chはルータの設定どおり。

## 5. `survey dump` と `fcsfail` が使えるか

```bash
sudo .venv/bin/python -m wifiwatch.hwprobe --interface wlan0 survey
sudo iw dev wlan0 set monitor fcsfail && sudo .venv/bin/python -m wifiwatch.hwprobe --interface wlan0 frames --ch 6g37 --seconds 10
```

- 合格（survey）：使用中のchに noise と busy の時間が出る。
- 合格（fcsfail）：エラーにならず、`FCS不良` が数えられる（0でもよい）。使えるなら `config.json` の `radio.monitor_flags` に `"fcsfail"` を入れる。

## 6. 頻繁なチャンネル切替でファームウェアが不安定にならないか

```bash
sudo .venv/bin/python -m wifiwatch.hwprobe --interface wlan0 switch --count 2000 --interval 0.25
```

- 合格：失敗0。切替にかかる時間の95%が仮定（30ms）を大きく超えない（超えたら `schedule.switch_s` を実測に合わせる）。
- 続けて本体をLIVEで1時間動かし、画面のイベント（受信の見張り）に「モニタを再設定」が出ないこと。

## 7. アンテナとケーブルの6GHzでの損失

用意：位置を固定した6GHzのAP（自チームのルータ）を、2m先に置く。

```bash
sudo .venv/bin/python -m wifiwatch.hwprobe --interface wlan0 own-rssi --bssid <ルータのBSSID> --ch 6g37 --minutes 2
```

延長ケーブル（RG58 1m）ありとなしで1回ずつ測り、中央値の差を見る。

- 目安：差が約2.5dB（仕様書の推定：延長なし約1.25dB、あり約3.75dB）。大きく違えば感度の見込み（約130m）を直す。

## 8. iwlwifi を読み込み直した直後に、6GHzが disabled になるか

```bash
sudo .venv/bin/python -m wifiwatch.hwprobe --interface wlan0 reload-check
```

- 記録：読み込み直した直後と、管理モードでスキャンしたあとの「6GHz 有効/無効」。
- 合格：スキャンのあとに24ch有効に戻る（受信停止の復旧手順がそのまま使える）。読み込み直しでIF名が変わる場合は、udevでIF名を固定する。

## 9. ピットとフィールドの差（約9dBの余裕に収まるか）

自チームの試合中、本体は動かしたまま（止めない）。自APの強さが `data/own.csv` に10秒ごとに残る（`config.json` の `own.bssids` が要る）。
試合の前後に、操縦者スマホ（Pixel）の歩測でフィールド際の自APの強さを測る。

- 記録：試合中のピットでの自APの強さ（中央値・最小）と、フィールド際での強さ。
- 合格：差が約9dB以内（仕様書：他の電波が自機に効き始める−82dBmと検出下限−91dBmの差）。超えるなら、フィールドで効く局がピットから見えない可能性がある（歩測で補う範囲を広げる）。

## 10. 処理能力（ミニPC）

```bash
cd ~/wifi-watch/server && .venv/bin/python tools/bench.py 10                       # 合成フレーム
sudo .venv/bin/python -m wifiwatch.hwprobe --interface wlan0 bench --ch 6g37 --width 160 --seconds 30   # 実際の受信
```

- 合格：合成で毎秒5000フレーム以上（開発PCは約12万）。実際の受信で、解析のCPU時間が受信時間の50%未満。
- 記録：両方の数字を `PROGRESS.md` に。

## 11. テザリングを切ったとき、外部の死活監視が3分で部員に通知するか

用意：healthchecks.io などに「期間1分・猶予2分」のチェックを作り、URLを `notify.healthcheck_url` に書く。通知先を部員にする。

- 手順：本体を動かしたまま、テザリング用スマホのモバイル通信を切る。
- 合格：3分前後で死活監視サービスから部員に通知が届く。戻すと、溜まっていたntfyの通知（crit・warn）が届く。

## 12. アプリからの指示が、中継を通ってPCに何秒で届くか（テザリング回線越し）

本番の中継（`docs/relay.md` のデプロイのあと）で、アプリから手動スキャンを10回送る。

- 記録：画面のイベント「アプリの指示を受け取った（中継で受け付けてからX秒）」の値（中央値と最大）。アプリを開いて10秒たってからの値（2秒間隔の状態）で測る。
- 目安：2〜3秒以内（問い合わせの間隔2秒＋往復）。開いた直後は最大で約10秒（待機中の間隔）。

## 13. 自作アプリ（Pixel 9a/8a）

M8で書き足す（スキャンの新しさ、6GHzのAPが一覧に出るか、PSC以外の6GHz局、IEが取れるか、ロボットのWi-Fiにつないだままモバイル回線で中継に届くか）。

## 14. ピットの警報音

```bash
printf C > /dev/ttyACM0
```

- 合格：鳴る（`docs/buzzer.md`）。本体では crit の発生・追加で同じ1バイトを送る。

## 記録

| 項目 | 日付 | 結果 | 数字・メモ |
| --- | --- | --- | --- |
| 1. 合否ゲート | | | 160MHz： |
| 2. 6GHzの受信・強さの合成 | | | |
| 3. 他局の160MHz送信の受信 | | | |
| 4. EHT Operation（320MHz） | | | |
| 5. survey・fcsfail | | | |
| 6. 切替の安定性 | | | 切替の時間 中央値／95%： |
| 7. 延長ケーブルの損失 | | | 差： dB |
| 8. 読み込み直しと6GHz | | | 直後： ／スキャン後： |
| 9. ピットとフィールドの差 | | | ピット： ／フィールド： |
| 10. 処理能力 | | | 合成： ／実受信： |
| 11. 死活監視 | | | 通知まで： 分 |
| 12. 指示が届くまで | | | 中央値： 秒／最大： 秒 |
| 14. 警報音 | | | |
