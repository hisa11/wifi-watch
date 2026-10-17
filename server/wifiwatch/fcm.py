"""FCM（Firebase Cloud Messaging）の HTTP v1 で、wifiwatch のアプリに通知を送る（中継は通さない）。

- サービスアカウントの鍵（Firebase のコンソールで作る JSON。秘密なのでコミットしない）で JWT（RS256）を作り、
  https://oauth2.googleapis.com/token でアクセストークン（1時間）に替えて使う（2026-10-17
  developers.google.com/identity/protocols/oauth2/service-account で確認）。
- 送り先は https://fcm.googleapis.com/v1/projects/{project_id}/messages:send、宛先はトピック（アプリが起動時に登録する）。
  緊急・警告は android.priority=high（スマホが休んでいても起こす。firebase.google.com/docs/cloud-messaging/android-message-priority）。
- 音・バイブ・画面の上に出すかは、アプリが作る通知チャネル（channel_id）で決まる。
"""
from __future__ import annotations

import base64
import json
import time
from pathlib import Path
from typing import Any

import httpx

SCOPE = "https://www.googleapis.com/auth/firebase.messaging"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SEND_URL = "https://fcm.googleapis.com/v1/projects/{project}/messages:send"

# アプリ（android/…/push/Channels.kt）と同じ名前
CHANNELS = {"crit": "alert_crit", "warn": "alert_warn", "info": "alert_info", "resolved": "alert_resolved"}


class FcmError(Exception):
    def __init__(self, msg: str, retry_after: float | None = None, permanent: bool = False):
        super().__init__(msg)
        self.retry_after = retry_after
        self.permanent = permanent


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def message(topic: str, level: str | None, kind: str, title: str, body: str, tag: str) -> dict[str, Any]:
    """送る中身。通知（タイトル・本文）はアプリが閉じていても通知欄に出る。data はアプリが開いているときに自分で出す用。"""
    chan = CHANNELS["resolved"] if kind == "resolved" else CHANNELS.get(level or "info", CHANNELS["info"])
    urgent = kind != "resolved" and level in ("crit", "warn")
    return {
        "topic": topic,
        "notification": {"title": title[:200], "body": body[:1000]},
        "data": {"level": level or "", "kind": kind, "title": title[:200], "body": body[:1000], "tag": tag, "channel": chan},
        "android": {"priority": "high" if urgent else "normal", "ttl": "3600s",
                    "notification": {"channel_id": chan, "tag": tag}},
    }


class FcmSender:
    def __init__(self, key_path: str, timeout: float = 10.0, transport: httpx.BaseTransport | None = None):
        key = json.loads(Path(key_path).read_text(encoding="utf-8"))
        for k in ("project_id", "client_email", "private_key"):
            if not key.get(k):
                raise ValueError(f"FCMの鍵（{key_path}）に {k} がありません。Firebaseのサービスアカウントの鍵（JSON）を指定してください")
        from cryptography.hazmat.primitives import serialization
        self.project = key["project_id"]
        self.email = key["client_email"]
        self.token_uri = key.get("token_uri") or TOKEN_URL
        self._key = serialization.load_pem_private_key(key["private_key"].encode("utf-8"), password=None)
        self.client = httpx.Client(timeout=timeout, transport=transport, headers={"User-Agent": "wifiwatch/0.1"})
        self._token: str | None = None
        self._token_exp = 0.0

    def _jwt(self, now: float) -> str:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        head = _b64(json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")).encode())
        claims = _b64(json.dumps({"iss": self.email, "scope": SCOPE, "aud": self.token_uri,
                                  "iat": int(now), "exp": int(now) + 3600}, separators=(",", ":")).encode())
        sig = self._key.sign(f"{head}.{claims}".encode("ascii"), padding.PKCS1v15(), hashes.SHA256())
        return f"{head}.{claims}.{_b64(sig)}"

    def _access_token(self, force: bool = False) -> str:
        now = time.time()
        if self._token and not force and now < self._token_exp - 60:
            return self._token
        try:
            r = self.client.post(self.token_uri, data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                                                       "assertion": self._jwt(now)})
        except httpx.HTTPError as e:
            raise FcmError(f"トークン：{type(e).__name__}: {e}") from None
        if r.status_code != 200:
            raise FcmError(f"トークン：HTTP {r.status_code}", permanent=r.status_code in (400, 401, 403))
        d = r.json()
        self._token = d["access_token"]
        self._token_exp = now + float(d.get("expires_in", 3600))
        return self._token

    def send(self, msg: dict[str, Any]) -> None:
        url = SEND_URL.format(project=self.project)
        for attempt in (0, 1):
            tok = self._access_token(force=attempt == 1)
            try:
                r = self.client.post(url, json={"message": msg}, headers={"Authorization": f"Bearer {tok}"})
            except httpx.HTTPError as e:
                raise FcmError(f"{type(e).__name__}: {e}") from None
            if r.status_code == 401 and attempt == 0:
                continue  # トークンが切れていた：作り直してもう一度
            if r.status_code == 429 or r.status_code >= 500:
                ra = r.headers.get("retry-after")
                raise FcmError(f"HTTP {r.status_code}", float(ra) if ra and ra.isdigit() else None)
            if r.status_code >= 300:
                raise FcmError(f"HTTP {r.status_code} {r.text[:200]}", permanent=True)
            return
