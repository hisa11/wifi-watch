"""wifiwatch のアプリへのプッシュ通知（FCM HTTP v1）。Google には送らず、偽の口（httpx の MockTransport）で確かめる。
鍵は試し用にその場で作る（本物の鍵はコミットしない）。"""
import base64
import json

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from wifiwatch import fcm
from wifiwatch.analyzer.alerts import AlertEvent
from wifiwatch.clock import FakeClock
from wifiwatch.config import NotifyConfig
from wifiwatch.notify import Buzzer, Notifier
from wifiwatch.store import Store


def b64d(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


@pytest.fixture
def keyfile(tmp_path):
    k = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    p = tmp_path / "sa.json"
    p.write_text(json.dumps({"type": "service_account", "project_id": "ww-test", "private_key": pem,
                             "client_email": "ww@ww-test.iam.gserviceaccount.com", "token_uri": fcm.TOKEN_URL}))
    return str(p), k.public_key()


class FakeGoogle:
    """トークンの口と送信の口。JWT の署名と中身を確かめ、送られたメッセージを覚える。"""

    def __init__(self, pub, send_status=None):
        self.pub = pub
        self.sent = []
        self.tokens = 0
        self.send_status = list(send_status or [])

    def __call__(self, req: httpx.Request) -> httpx.Response:
        if str(req.url) == fcm.TOKEN_URL:
            form = dict(x.split("=", 1) for x in req.content.decode().split("&"))
            assert form["grant_type"] == "urn%3Aietf%3Aparams%3Aoauth%3Agrant-type%3Ajwt-bearer"
            head, claims, sig = form["assertion"].split(".")
            self.pub.verify(b64d(sig), f"{head}.{claims}".encode(), padding.PKCS1v15(), hashes.SHA256())  # 署名が正しい
            h, c = json.loads(b64d(head)), json.loads(b64d(claims))
            assert h == {"alg": "RS256", "typ": "JWT"}
            assert c["iss"] == "ww@ww-test.iam.gserviceaccount.com" and c["aud"] == fcm.TOKEN_URL
            assert c["scope"] == fcm.SCOPE and 0 < c["exp"] - c["iat"] <= 3600
            self.tokens += 1
            return httpx.Response(200, json={"access_token": f"tok{self.tokens}", "expires_in": 3600, "token_type": "Bearer"})
        assert str(req.url) == "https://fcm.googleapis.com/v1/projects/ww-test/messages:send"
        if self.send_status:
            st = self.send_status.pop(0)
            if st != 200:
                return httpx.Response(st, headers={"retry-after": "7"} if st == 429 else {})
        self.sent.append((req.headers["authorization"], json.loads(req.content)["message"]))
        return httpx.Response(200, json={"name": "projects/ww-test/messages/1"})


def ev(kind="raised", level="warn"):
    return AlertEvent(1_791_936_000.0, kind, "offchannel", "offchannel:x", level, "割当違反（予約外ch）",
                      "A高専：X が 6GHz ch45 に出ています", [{"ssid": "X", "ch": "6GHz ch45", "bssid": "02:00:00:00:00:09"}], True)


def make(tmp_path, keyfile, google):
    sender = fcm.FcmSender(keyfile[0], transport=httpx.MockTransport(google))
    clk = FakeClock()
    store = Store(tmp_path / "w.db")
    buzz = tmp_path / "tty"
    buzz.write_bytes(b"")
    n = Notifier(NotifyConfig(timeout_s=2), store, clk, buzzer=Buzzer(str(buzz)), fcm_sender=sender)
    return n, store


def test_alerts_go_to_the_app_with_sound_channels(tmp_path, keyfile):
    g = FakeGoogle(keyfile[1])
    n, store = make(tmp_path, keyfile, g)
    n.handle_alert(ev(level="warn"))
    n.handle_alert(ev(level="crit"))
    n.handle_alert(ev(kind="resolved", level="warn"))
    n.heartbeat()  # 稼働中はアプリには送らない（ntfy の設定が無いので、どこにも送らない）
    n.deliver_due()
    msgs = [m for _, m in g.sent]
    assert [m["android"]["notification"]["channel_id"] for m in msgs] == ["alert_warn", "alert_crit", "alert_resolved"]
    assert [m["android"]["priority"] for m in msgs] == ["high", "high", "normal"]  # 緊急・警告はスマホが休んでいても起こす
    assert all(m["topic"] == "wifiwatch-alerts" for m in msgs)
    assert msgs[0]["notification"]["title"] == "警告｜割当違反（予約外ch）"
    assert msgs[1]["notification"]["title"].startswith("緊急｜")
    assert msgs[2]["notification"]["title"].startswith("解消｜")
    assert msgs[0]["android"]["notification"]["tag"] == msgs[2]["android"]["notification"]["tag"]  # 解消で置き換わる
    assert all(isinstance(v, str) for m in msgs for v in m["data"].values())  # data の値は文字列
    assert g.tokens == 1 and g.sent[0][0] == "Bearer tok1"  # トークンは使い回す
    assert store.notify_backlog() == 0
    store.close()


def test_retries_until_delivered_and_refreshes_expired_token(tmp_path, keyfile):
    g = FakeGoogle(keyfile[1], send_status=[503, 401])
    n, store = make(tmp_path, keyfile, g)
    n.handle_alert(ev(level="crit"))
    assert n.deliver_due() == 0 and store.notify_backlog() == 1  # 失敗しても消さない
    n.clock.advance(5)
    assert n.deliver_due() == 1  # 401 → トークンを作り直して届く
    assert g.tokens == 2 and g.sent[0][0] == "Bearer tok2"
    store.close()


def test_rate_limit_and_bad_message_do_not_block_forever(tmp_path, keyfile):
    g = FakeGoogle(keyfile[1], send_status=[429])
    sender = fcm.FcmSender(keyfile[0], transport=httpx.MockTransport(g))
    with pytest.raises(fcm.FcmError) as e:
        sender.send(fcm.message("wifiwatch-alerts", "warn", "raised", "t", "b", "k"))
    assert e.value.retry_after == 7 and not e.value.permanent
    g.send_status = [400, 400, 400]
    n, store = make(tmp_path, keyfile, g)
    n.handle_alert(ev(level="warn"))
    n.handle_alert(ev(level="crit"))
    for _ in range(4):
        n.deliver_due()
        n.clock.advance(120)
    # 中身が悪いもの（400）は3回で諦め、後ろの警報は届く
    assert [m["android"]["notification"]["channel_id"] for _, m in g.sent] == ["alert_crit"]
    store.close()


def test_bad_key_does_not_stop_other_notifications(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{}")
    n = Notifier(NotifyConfig(fcm_service_account=str(bad)), Store(tmp_path / "w.db"), FakeClock(), buzzer=Buzzer(None))
    assert n.fcm is None and "鍵" in n.fcm_error
    n.handle_alert(ev(level="crit"))  # 落ちない
    assert n.info()["fcm"] is False
