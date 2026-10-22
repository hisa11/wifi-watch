"""ローカルAPIとダッシュボード（127.0.0.1だけ。デスクトップの画面専用）。

ブラウザを経由したなりすましを防ぐ：
- Hostヘッダが 127.0.0.1 か localhost（とポート）の要求だけ受ける（DNS rebinding対策）。
- 書き込み（POST/PUT/PATCH/DELETE）は独自ヘッダ `X-Wifiwatch: 1` 付きだけ受ける（CSRF対策。
  他のサイトのページはこのヘッダを付けると事前確認になり、CORSを許していないので送れない）。
- Originヘッダがあれば、自分（http://127.0.0.1:ポート / http://localhost:ポート）以外の書き込みは断る。
- 画面は外部のスクリプトや資源を読まない（CSP default-src 'self'）。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from .controller import ControlError, Controller

log = logging.getLogger(__name__)

STATIC = Path(__file__).resolve().parent / "static"
WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
       "font-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")


def _err(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": code, "message": message}, status_code=status)


def create_app(ctl: Controller, port: int) -> FastAPI:
    app = FastAPI(title="wifiwatch", docs_url=None, redoc_url=None, openapi_url=None)
    allowed_hosts = {"127.0.0.1", "localhost", f"127.0.0.1:{port}", f"localhost:{port}"}
    allowed_origins = {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}

    @app.middleware("http")
    async def guard(request: Request, call_next: Any) -> Response:
        host = (request.headers.get("host") or "").lower()
        if host not in allowed_hosts:
            return _err(403, "bad_host", "Hostヘッダが 127.0.0.1 / localhost ではありません")
        if request.method in WRITE_METHODS:
            if request.headers.get("x-wifiwatch") != "1":
                return _err(403, "missing_header", "書き込みには X-Wifiwatch: 1 ヘッダが要ります")
            origin = request.headers.get("origin")
            if origin is not None and origin not in allowed_origins:
                return _err(403, "bad_origin", "別のサイトからの書き込みは受けません")
        if request.method == "OPTIONS":
            return _err(403, "no_cors", "CORSは許していません")
        resp: Response = await call_next(request)
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Content-Security-Policy"] = CSP
        resp.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        if request.url.path.startswith("/api/"):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.exception_handler(ControlError)
    async def control_error(request: Request, exc: ControlError) -> JSONResponse:
        return _err(exc.status, exc.code, exc.message)

    async def body(request: Request) -> dict[str, Any]:
        try:
            data = await request.json()
        except Exception:
            raise ControlError(400, "bad_json", "JSONが読めません") from None
        if not isinstance(data, dict):
            raise ControlError(400, "bad_json", "JSONのオブジェクトを送ってください")
        return data

    # -------------------------------------------------------------- 状態
    @app.get("/api/state")
    def state() -> Any:
        return ctl.state()

    @app.get("/api/stations")
    def stations() -> Any:
        return ctl.stations()

    @app.get("/api/stations/{bssid}")
    def station(bssid: str) -> Any:
        return ctl.station_detail(bssid.lower())

    @app.get("/api/diag/rssi")
    def diag_rssi() -> Any:
        return ctl.diag_rssi()

    @app.get("/api/clients")
    def clients() -> Any:
        return ctl.clients()

    @app.get("/api/events")
    def events(limit: int = 200, since_id: int = 0, category: str | None = None) -> Any:
        return ctl.store.events(max(1, min(limit, 1000)), since_id, category)

    # -------------------------------------------------------------- 手動スキャン
    @app.post("/api/scan")
    async def scan(request: Request) -> Any:
        d = await body(request)
        return ctl.manual_scan(d.get("bands"), d.get("freqs"), bool(d.get("confirm", False)), "desktop",
                               d.get("request_id"))

    @app.get("/api/scan/{scan_id}")
    def scan_status(scan_id: str) -> Any:
        return ctl.scan_status(scan_id)

    # -------------------------------------------------------------- 帯域ON/OFF
    @app.get("/api/bands")
    def bands() -> Any:
        return ctl.bands()

    @app.put("/api/bands")
    async def set_bands(request: Request) -> Any:
        d = await body(request)
        ctl.set_bands(d.get("excluded_bands", []), d.get("excluded_freqs", []), "desktop", d.get("request_id"))
        return ctl.bands()

    # -------------------------------------------------------------- 記録とラベル
    @app.get("/api/records")
    def records(kind: str | None = None, since: float | None = None, until: float | None = None,
                location: str | None = None, band: str | None = None, tag: str | None = None,
                limit: int = 200) -> Any:
        return ctl.list_records(kind=kind, since=since, until=until, location=location, band=band, tag=tag, limit=limit)

    @app.get("/api/records/export")
    def export(format: str = "json", kind: str | None = None, since: float | None = None,
               until: float | None = None, location: str | None = None, band: str | None = None) -> Response:
        ctype, data, name = ctl.export(format, kind=kind, since=since, until=until, location=location, band=band)
        return Response(data, media_type=ctype, headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @app.get("/api/records/{rid}")
    def record(rid: str) -> Any:
        return ctl.record_for_screen(rid)

    @app.get("/api/records/{rid}/stations")
    def record_stations(rid: str, bssid: list[str] = Query(default=[])) -> Any:
        return ctl.record_stations(rid, bssid)

    @app.patch("/api/records/{rid}")
    async def label(rid: str, request: Request) -> Any:
        d = await body(request)
        return ctl.update_label(rid, d, "desktop")

    # -------------------------------------------------------------- デスクトップだけの操作
    @app.post("/api/mode")
    async def mode(request: Request) -> Any:
        d = await body(request)
        return ctl.set_mode(str(d.get("mode")), "desktop")

    @app.post("/api/own")
    async def own(request: Request) -> Any:
        d = await body(request)
        return ctl.set_own(d.get("channel"), d.get("width"), "desktop")

    @app.post("/api/tags")
    async def tags(request: Request) -> Any:
        d = await body(request)
        return ctl.tag(str(d.get("bssid", "")).lower(), d.get("team"), "desktop")

    # 自APのchに自chを自動で合わせるか（デスクトップだけ）
    @app.post("/api/own_follow")
    async def own_follow(request: Request) -> Any:
        d = await body(request)
        return ctl.set_own_follow(d.get("enabled"), "desktop")

    # 自チームのAP：MACアドレス（BSSID）かSSIDで登録する（デスクトップだけ）
    @app.post("/api/own_ap")
    async def own_ap(request: Request) -> Any:
        d = await body(request)
        return ctl.set_own_ap(d.get("bssids", []), d.get("ssids", []), "desktop")

    # 機種の覚え帳：指紋ごとに機種の名前を覚える（同じ指紋の局にも出る）。label を null にすると忘れる
    @app.get("/api/models")
    def models() -> Any:
        return ctl.model_book()

    @app.post("/api/models")
    async def remember_model(request: Request) -> Any:
        d = await body(request)
        b = d.get("bssid")
        return ctl.remember_model(d.get("fp"), d.get("label"), b.lower() if isinstance(b, str) else None, "desktop")

    # -------------------------------------------------------------- 画面
    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app
