"""Real local CRUD/auth backend for the integration delivery acceptance task."""
from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

ITEMS = [{"id": 1, "title": "初始任务"}]
LOCK = threading.RLock()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def reply(self, status, value, *, cookie=None):
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Fixture-Trace", "real-local-backend")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(body)
        print(f"backend/server.py:22 {self.command} {self.path} status={status}", flush=True)

    def payload(self):
        size = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(min(size, 65536)) or b"{}")

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/health":
            marker = os.environ.get("HEALTH_SWITCH_FILE")
            if marker and Path(marker).is_file():
                return self.reply(503, {"ok": False, "role": "backend", "message": "controlled health degradation"})
            return self.reply(200, {"ok": True, "role": "backend"})
        if path == "/api/items":
            with LOCK:
                return self.reply(200, {"items": list(ITEMS)})
        if path == "/api/private":
            authorization = self.headers.get("Authorization") == "Bearer fixture-token"
            cookie = "fixture_sid=integration-cookie" in self.headers.get("Cookie", "")
            return self.reply(200 if authorization and cookie else 401,
                              {"authenticated": authorization and cookie, "cookie_seen": cookie, "authorization_seen": authorization})
        if path == "/api/xhr":
            return self.reply(200, {"transport": "xhr"})
        if path == "/api/error":
            return self.reply(500, {"message": "可恢复的示例错误"})
        return self.reply(404, {"message": "Not found"})

    def do_POST(self):
        if self.path == "/api/login":
            self.payload()
            return self.reply(200, {"token": "fixture-token"},
                              cookie="fixture_sid=integration-cookie; Path=/; HttpOnly; SameSite=Lax")
        if self.path == "/api/items":
            data = self.payload()
            with LOCK:
                item = {"id": max((item["id"] for item in ITEMS), default=0) + 1, "title": str(data.get("title", ""))}
                ITEMS.append(item)
                return self.reply(201, item)
        return self.reply(404, {"message": "Not found"})

    def do_PATCH(self):
        data = self.payload()
        with LOCK:
            item = next((item for item in ITEMS if self.path == f"/api/items/{item['id']}"), None)
            if item is not None:
                item["title"] = str(data.get("title", item["title"]))
                return self.reply(200, item)
        return self.reply(404, {"message": "Item not found"})

    def do_DELETE(self):
        with LOCK:
            for index, item in enumerate(ITEMS):
                if self.path == f"/api/items/{item['id']}":
                    ITEMS.pop(index)
                    return self.reply(200, {"deleted": item["id"]})
        return self.reply(404, {"message": "Item not found"})


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", int(os.environ["PORT"])), Handler)
    print(f"backend/server.py:88 backend listening port={server.server_port}", flush=True)
    server.serve_forever(poll_interval=0.1)
