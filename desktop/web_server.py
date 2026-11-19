# -*- coding: utf-8 -*-
"""本地面板服务：127.0.0.1 + token 鉴权 + 静态面板 + JSON API。

只服务本机回环地址；页面本身无敏感数据，所有控制走 /api 且必须携带
``X-Token``（面板 URL 里带的随机 token）。不做 CORS，不接受外部 Host。
"""
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse


class UIState:
    """极小的 UI 偏好存储（当前只有主题）；原子写，失败静默。"""

    def __init__(self, path):
        self.path = path
        self._lock = threading.RLock()
        self._data = self._read()

    def _read(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def theme(self):
        with self._lock:
            value = self._data.get("theme")
            return value if value in ("light", "dark") else None

    def _save_locked(self):
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except OSError:
            return False
        return True

    def set_theme(self, value):
        if value not in ("light", "dark"):
            return False
        with self._lock:
            self._data["theme"] = value
            return self._save_locked()

    def window_size(self):
        """用户自定义窗口大小;没有或非法时返回 None(调用方用默认值)。"""
        with self._lock:
            win = self._data.get("window")
            if not isinstance(win, dict):
                return None
            try:
                w, h = int(win.get("width")), int(win.get("height"))
            except (TypeError, ValueError):
                return None
            if 640 <= w <= 10000 and 480 <= h <= 10000:
                return (w, h)
            return None

    def set_window_size(self, width, height):
        try:
            w, h = int(width), int(height)
        except (TypeError, ValueError):
            return False
        if not (640 <= w <= 10000 and 480 <= h <= 10000):
            return False
        with self._lock:
            self._data["window"] = {"width": w, "height": h}
            return self._save_locked()


class PanelServer:
    """供预览和真实 app 共用的本地 HTTP 服务。"""

    def __init__(self, runtime, panel_path, token, port=0, read_only=True,
                 actions=None, ui_state=None):
        self.runtime = runtime
        self.panel_path = panel_path
        self.token = token
        self.port = port
        self.read_only = read_only
        self.actions = actions or {}
        self.ui_state = ui_state
        self._httpd = None
        self._thread = None

    def start(self):
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt, *args):
                pass

            # ---- helpers ----
            def _host_ok(self):
                port = outer._httpd.server_address[1] if outer._httpd else 0
                host = (self.headers.get("Host") or "").strip().lower()
                return host in ("127.0.0.1:%d" % port,
                                "localhost:%d" % port)

            def _authorized(self):
                return (self.headers.get("X-Token") or "") == outer.token

            def _send_bytes(self, code, body, ctype):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def _json(self, obj, code=200):
                body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
                self._send_bytes(code, body, "application/json; charset=utf-8")

            def _error(self, code, message):
                self._json({"ok": False, "error": message}, code)

            def _read_json(self):
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    n = 0
                if n <= 0:
                    return {}
                raw = self.rfile.read(min(n, 64 * 1024))
                try:
                    data = json.loads(raw.decode("utf-8", "replace"))
                    return data if isinstance(data, dict) else {}
                except ValueError:
                    return {}

            def _snapshot(self):
                snap = outer.runtime.snapshot()
                snap["readOnly"] = bool(outer.read_only or
                                        getattr(outer.runtime, "read_only", False))
                snap["theme"] = (outer.ui_state.theme()
                                 if outer.ui_state is not None else None)
                return snap

            # ---- routes ----
            def do_GET(self):
                if not self._host_ok():
                    return self._error(400, "bad host")
                path = urlparse(self.path).path
                if path in ("/", "/index.html", "/panel.html"):
                    try:
                        with open(outer.panel_path, "rb") as f:
                            body = f.read()
                    except OSError as e:
                        return self._error(500, "panel unavailable: %s" % e)
                    return self._send_bytes(200, body,
                                            "text/html; charset=utf-8")
                if path == "/api/state":
                    if not self._authorized():
                        return self._error(401, "bad token")
                    return self._json(self._snapshot())
                if path == "/api/ping":
                    if not self._authorized():
                        return self._error(401, "bad token")
                    return self._json({
                        "ok": True,
                        "pid": os.getpid(),
                        "version": getattr(outer.runtime, "version", ""),
                        "readOnly": bool(outer.read_only),
                    })
                return self._error(404, "not found")

            def do_POST(self):
                if not self._host_ok():
                    return self._error(400, "bad host")
                if not self._authorized():
                    return self._error(401, "bad token")
                path = urlparse(self.path).path
                if path in ("/api/pause", "/api/resume", "/api/test-send"):
                    if outer.read_only:
                        return self._error(409, "read-only preview")
                    fn = outer.actions.get(path.rsplit("/", 1)[-1].replace("-", "_"))
                    if fn is None:
                        return self._error(501, "not implemented")
                    try:
                        result = fn()
                    except Exception as e:
                        return self._error(500, "action failed: %s" % e)
                    return self._json({"ok": True if result is None else bool(result)})
                if path in ("/api/open-log", "/api/open-config"):
                    key = "open_log" if path.endswith("log") else "open_config"
                    fn = outer.actions.get(key)
                    if fn is None:
                        return self._error(501, "not implemented")
                    try:
                        ok = fn()
                    except Exception as e:
                        return self._error(500, "action failed: %s" % e)
                    return self._json({"ok": bool(ok)})
                if path == "/api/theme":
                    data = self._read_json()
                    theme = data.get("theme")
                    if outer.ui_state is None:
                        return self._error(501, "theme store unavailable")
                    if not outer.ui_state.set_theme(theme):
                        return self._error(400, "invalid theme")
                    return self._json({"ok": True, "theme": theme})
                return self._error(404, "not found")

        self._httpd = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        self._httpd.daemon_threads = True
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                        name="panel-http", daemon=True)
        self._thread.start()
        return self.port

    def stop(self):
        if self._httpd is not None:
            try:
                self._httpd.shutdown()
            except Exception:
                pass
            try:
                self._httpd.server_close()
            except Exception:
                pass
            self._httpd = None
        if self._thread is not None:
            try:
                self._thread.join(timeout=2.0)
            except Exception:
                pass
            self._thread = None

    def url(self):
        return "http://127.0.0.1:%d/?token=%s" % (self.port, self.token)
