"""テスト用の偽のHTTPサーバ（ntfy・Discord・死活監視・中継の代わり）。127.0.0.1だけで待つ。"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeHttp:
    def __init__(self):
        self.requests = []  # (method, path, json or None)
        self.mode = "ok"  # ok / fail / 429
        self.port = 0
        self.server = None
        self.thread = None

    def start(self, port=0):
        owner = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _reply(self, method):
                n = int(self.headers.get("content-length") or 0)
                body = self.rfile.read(n) if n else b""
                if owner.mode == "fail":
                    self.send_response(503)
                    self.end_headers()
                    return
                if owner.mode == "429":
                    self.send_response(429)
                    self.send_header("Retry-After", "5")
                    self.end_headers()
                    return
                data = json.loads(body.decode("utf-8")) if body else None
                owner.requests.append((method, self.path, data))
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.end_headers()
                self.wfile.write(b"{}")

            def do_POST(self):
                self._reply("POST")

            def do_GET(self):
                self._reply("GET")

        self.server = ThreadingHTTPServer(("127.0.0.1", port), H)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.server = None

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}"

    def posts(self, path=None):
        return [d for m, p, d in self.requests if m == "POST" and (path is None or p == path)]
