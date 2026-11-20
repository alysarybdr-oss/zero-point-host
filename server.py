import http.server
import socketserver
import json
import os
import io
import cgi
import hashlib
import hmac
import urllib.parse
import urllib.request
import urllib.error
from pathlib import Path

import storage
import runtime
from core.security import is_valid_pid, safe_project_path

BASE_DIR = Path(__file__).parent
STATIC_DIR = BASE_DIR / "static"

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", 8080))
MAX_UPLOAD_MB = int(os.environ.get("ZP_MAX_UPLOAD_MB", 200))
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024

ADMIN_PASSWORD = os.environ.get("ZP_ADMIN_PASSWORD", "")
SESSION_COOKIE = "zp_session"


def _make_token():
    return hmac.new(
        (ADMIN_PASSWORD or "default").encode(),
        os.urandom(16),
        hashlib.sha256
    ).hexdigest()


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path, content_type):
        with open(path, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authed(self):
        if not ADMIN_PASSWORD:
            return True
        cookie = self.headers.get("Cookie", "")
        for part in cookie.split(";"):
            k, _, v = part.strip().partition("=")
            if k == SESSION_COOKIE:
                expected = _make_token()
                return hmac.compare_digest(v, expected)
        return False

    def _require_auth(self):
        if not self._authed():
            self._json(401, {"error": "unauthorized"})
            return False
        return True

    def _pid_from(self, path, suffix):
        if not path.endswith(suffix):
            return None
        parts = path.split("/")
        if len(parts) < 5:
            return None
        pid = parts[3]
        return pid if is_valid_pid(pid) else None

    def _proxy(self, port, path, method="GET"):
        url = f"http://127.0.0.1:{port}{path}"
        try:
            req = urllib.request.Request(url, method=method)
            for h in ("Content-Type", "Accept", "X-Session-Token"):
                if self.headers.get(h):
                    req.add_header(h, self.headers[h])
            length = int(self.headers.get("Content-Length", 0))
            data = self.rfile.read(length) if length else None
            with urllib.request.urlopen(req, data=data, timeout=60) as r:
                body = r.read()
                self.send_response(r.status)
                for k, v in r.headers.items():
                    if k.lower() in ("transfer-encoding", "connection", "content-length"):
                        continue
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body)
        except urllib.error.HTTPError as e:
            body = e.read()
            self.send_response(e.code)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:
            self._json(502, {"error": f"proxy failed: {e}"})

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path in ("/", "/index.html"):
            self._file(STATIC_DIR / "index.html", "text/html; charset=utf-8")
            return

        if path == "/api/health":
            self._json(200, {"ok": True, "auth_required": bool(ADMIN_PASSWORD)})
            return

        if not self._require_auth():
            return

        if path == "/api/projects":
            self._json(200, {"projects": storage.list_projects()})
            return

        pid = self._pid_from(path, "/files")
        if pid:
            proj = storage.get_project(pid)
            if not proj:
                self._json(404, {"error": "not found"}); return
            pdir = storage.PROJECTS_DIR / pid
            files = [{"path": str(f.relative_to(pdir)), "size": f.stat().st_size}
                     for f in pdir.rglob("*") if f.is_file()]
            self._json(200, {"files": files})
            return

        pid = self._pid_from(path, "/status")
        if pid:
            self._json(200, runtime.status(pid))
            return

        pid = self._pid_from(path, "/logs")
        if pid:
            self._json(200, {"log": runtime.logs(pid)})
            return

        if path.startswith("/app/"):
            parts = path.split("/", 3)
            if len(parts) >= 3:
                pid = parts[2]
                if not is_valid_pid(pid):
                    self._json(400, {"error": "invalid pid"}); return
                st = runtime.status(pid)
                if not st.get("running"):
                    self._json(503, {"error": "project not running"}); return
                rest = "/" + (parts[3] if len(parts) > 3 else "")
                q = ("?" + parsed.query) if parsed.query else ""
                self._proxy(st["port"], rest + q, method="GET")
                return

        self._json(404, {"error": "Not Found"})

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == "/api/login":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            data = json.loads(body) if body else {}
            if not ADMIN_PASSWORD or data.get("password") == ADMIN_PASSWORD:
                token = _make_token()
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Set-Cookie", f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Lax")
                self.end_headers()
                self.wfile.write(json.dumps({"ok": True}).encode())
            else:
                self._json(401, {"error": "wrong password"})
            return

        if not self._require_auth():
            return

        if path == "/api/projects/create":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            data = json.loads(body) if body else {}
            name = (data.get("name") or "Untitled").strip()[:80] or "Untitled"
            pid = storage.create_project(name)
            self._json(200, {"id": pid, "name": name})
            return

        pid = self._pid_from(path, "/upload")
        if pid:
            if not storage.get_project(pid):
                self._json(404, {"error": "project not found"}); return
            self._handle_upload(pid)
            return

        pid = self._pid_from(path, "/start")
        if pid:
            if not storage.get_project(pid):
                self._json(404, {"error": "project not found"}); return
            pdir = storage.PROJECTS_DIR / pid
            res = runtime.start(pid, pdir)
            if res.get("ok"):
                storage.update_project(pid, status="running", entry=res.get("entry"))
            self._json(200 if res.get("ok") else 500, res)
            return

        pid = self._pid_from(path, "/stop")
        if pid:
            res = runtime.stop(pid)
            storage.update_project(pid, status="stopped")
            self._json(200, res)
            return

        if path.startswith("/app/"):
            parts = path.split("/", 3)
            if len(parts) >= 3:
                pid = parts[2]
                if not is_valid_pid(pid):
                    self._json(400, {"error": "invalid pid"}); return
                st = runtime.status(pid)
                if not st.get("running"):
                    self._json(503, {"error": "project not running"}); return
                rest = "/" + (parts[3] if len(parts) > 3 else "")
                self._proxy(st["port"], rest, method="POST")
                return

        self._json(404, {"error": "Not Found"})

    def _handle_upload(self, pid):
        content_type = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in content_type:
            self._json(400, {"error": "expected multipart/form-data"}); return

        length = int(self.headers.get("Content-Length", 0))
        if length > MAX_UPLOAD_BYTES:
            self._json(413, {"error": f"upload too large (max {MAX_UPLOAD_MB} MB)"})
            return

        body = self.rfile.read(length)
        env = {
            "REQUEST_METHOD": "POST",
            "CONTENT_TYPE": content_type,
            "CONTENT_LENGTH": str(length),
        }
        fs = cgi.FieldStorage(
            fp=io.BytesIO(body),
            environ=env,
            headers=self.headers
        )

        saved, total = [], 0
        for key in fs.keys():
            items = fs[key] if isinstance(fs[key], list) else [fs[key]]
            for item in items:
                if not getattr(item, "filename", None):
                    continue
                dest = safe_project_path(storage.PROJECTS_DIR, pid, item.filename)
                if not dest:
                    continue
                data = item.file.read()
                dest.write_bytes(data)
                total += len(data)
                saved.append({"name": dest.name, "size": len(data)})

        storage.update_project(pid, status="uploaded", size=total, files=saved)
        self._json(200, {"uploaded": saved, "total": total})


class ThreadingServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


if __name__ == "__main__":
    print(f"ZERO POINT HOST v0.3 — http://{HOST}:{PORT}")
    print(f"Auth: {'مفعّل' if ADMIN_PASSWORD else 'مفتوح (اضبط ZP_ADMIN_PASSWORD)'}")
    print(f"Max upload: {MAX_UPLOAD_MB} MB")
    with ThreadingServer((HOST, PORT), Handler) as httpd:
        httpd.serve_forever()