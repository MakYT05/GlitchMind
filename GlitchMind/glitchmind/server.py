"""Локальный HTTP-сервер: отдаёт интерфейс, JSON-API и поток событий (SSE).

Слушает только 127.0.0.1, все запросы к API требуют одноразовый токен сессии
(защита от того, чтобы чужая веб-страница в браузере читала твой разговор).
"""
from __future__ import annotations

import hmac
import json
import mimetypes
import os
import queue
import secrets
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import paths
from .audio import capture
from .audio.capture import AudioError
from .controller import Controller
from .knowledge import kb
from .knowledge.parsers import ParseError
from .llm.client import LLMClient, LLMError

MAX_JSON = 2 * 1024 * 1024
MAX_UPLOAD = 200 * 1024 * 1024


def _default(o):
    if hasattr(o, "item"):
        return o.item()
    if isinstance(o, (set, tuple)):
        return list(o)
    return str(o)


def dumps(obj) -> bytes:
    return json.dumps(obj, ensure_ascii=False, default=_default).encode("utf-8")


class ApiServer:
    def __init__(self, ctl: Controller, host: str = "127.0.0.1", port: int = 0, on_quit=None):
        self.ctl = ctl
        self.token = secrets.token_urlsafe(18)
        self.on_quit = on_quit
        self._stopping = threading.Event()
        self.httpd = ThreadingHTTPServer((host, port), self._handler_class())
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/?t={self.token}"

    def start(self) -> None:
        self._thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.2},
                                        daemon=True, name="http")
        self._thread.start()

    def stop(self) -> None:
        self._stopping.set()
        try:
            self.httpd.shutdown()
            self.httpd.server_close()
        except Exception:  # noqa: BLE001
            pass

    # ---------------------------------------------------------------- handler
    def _handler_class(self):
        srv = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "GlitchMind"

            def log_message(self, *a):  # тишина в консоли
                pass

            # ---- вспомогательное
            def _send(self, code: int, body: bytes, ctype: str = "application/json; charset=utf-8",
                      extra: dict | None = None) -> None:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

            def _json(self, obj, code: int = 200) -> None:
                self._send(code, dumps(obj))

            def _err(self, code: int, msg: str) -> None:
                self._json({"error": msg}, code)

            def _host_ok(self) -> bool:
                host = (self.headers.get("Host") or "").lower()
                return host in (f"127.0.0.1:{srv.port}", f"localhost:{srv.port}")

            def _token_ok(self, query: dict) -> bool:
                tok = self.headers.get("X-Token") or (query.get("t") or [""])[0]
                return bool(tok) and hmac.compare_digest(tok, srv.token)

            def _origin_ok(self) -> bool:
                origin = self.headers.get("Origin")
                if not origin:
                    return True
                return origin.lower() in (f"http://127.0.0.1:{srv.port}", f"http://localhost:{srv.port}")

            def _body(self, limit: int = MAX_JSON) -> bytes:
                n = int(self.headers.get("Content-Length") or 0)
                if n > limit:
                    raise ValueError("Слишком большой запрос")
                return self.rfile.read(n) if n else b""

            def _json_body(self) -> dict:
                raw = self._body()
                if not raw:
                    return {}
                d = json.loads(raw.decode("utf-8"))
                return d if isinstance(d, dict) else {}

            # ---- GET
            def do_GET(self):  # noqa: N802
                u = urlparse(self.path)
                q = parse_qs(u.query)
                if not self._host_ok():
                    return self._err(403, "bad host")
                path = u.path
                if path == "/":
                    if not self._token_ok(q):
                        return self._send(403, "Откройте приложение через GlitchMind.exe".encode("utf-8"),
                                          "text/plain; charset=utf-8")
                    return self._index()
                if path.startswith("/static/"):
                    return self._static(path[len("/static/"):])
                if not self._token_ok(q):
                    return self._err(403, "forbidden")
                try:
                    if path == "/events":
                        return self._sse()
                    if path == "/skin":
                        return self._skin()
                    if path == "/api/state":
                        return self._json(srv.ctl.state())
                    if path == "/api/devices":
                        return self._json(capture.list_devices())
                    if path == "/api/kb":
                        return self._json({"books": kb.books()})
                    if path == "/api/library":
                        return self._json({"docs": srv.ctl.lib.docs(), "status": srv.ctl.lib.status()})
                    if path == "/api/library/search":
                        res = srv.ctl.lib.search((q.get("q") or [""])[0], 5)
                        return self._json({"results": res})
                    if path == "/api/llm/models":
                        s = srv.ctl.store.get()
                        c = LLMClient(s.llm_provider, s.llm_url, s.llm_model, s.llm_api_key, timeout=8)
                        try:
                            return self._json({"ok": True, "models": c.list_models()})
                        except LLMError as e:
                            return self._json({"ok": False, "models": [], "message": str(e)})
                except Exception as e:  # noqa: BLE001
                    return self._err(500, str(e))
                return self._err(404, "not found")

            def _index(self) -> None:
                f = paths.resource_dir() / "ui" / "index.html"
                try:
                    html = f.read_text(encoding="utf-8").replace("{{GM_TOKEN}}", srv.token)
                except OSError:
                    return self._err(500, "ui/index.html не найден")
                self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")

            def _static(self, rel: str) -> None:
                root = (paths.resource_dir() / "ui").resolve()
                f = (root / rel).resolve()
                if root not in f.parents or not f.is_file():
                    return self._err(404, "not found")
                ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
                if f.suffix in (".js", ".css", ".svg", ".html"):
                    ctype += "; charset=utf-8"
                self._send(200, f.read_bytes(), ctype)

            def _skin(self) -> None:
                f = paths.skin_file()
                if f is None:
                    return self._err(404, "no skin")
                ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
                self._send(200, f.read_bytes(), ctype)

            def _sse(self) -> None:
                q = srv.ctl.bus.subscribe()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()
                pings = 0
                try:
                    self.wfile.write(b": hello\n\n")
                    self.wfile.flush()
                    while not srv._stopping.is_set():
                        try:
                            msg = q.get(timeout=1.0)
                        except queue.Empty:
                            pings += 1
                            if pings % 10 == 0:
                                self.wfile.write(b": ping\n\n")
                                self.wfile.flush()
                            continue
                        data = dumps(msg["data"])
                        self.wfile.write(b"event: " + msg["type"].encode() + b"\ndata: " + data + b"\n\n")
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
                finally:
                    srv.ctl.bus.unsubscribe(q)

            # ---- POST
            def do_POST(self):  # noqa: N802
                u = urlparse(self.path)
                q = parse_qs(u.query)
                if not self._host_ok() or not self._origin_ok():
                    return self._err(403, "forbidden")
                if not self._token_ok(q):
                    return self._err(403, "forbidden")
                path = u.path
                ctl = srv.ctl
                try:
                    if path == "/api/library/upload":
                        name = os.path.basename((q.get("name") or ["book.txt"])[0])
                        data = self._body(MAX_UPLOAD)
                        try:
                            doc = ctl.lib.add_bytes(name, data)
                        except ParseError as e:
                            return self._err(400, str(e))
                        ctl.bus.publish("library", {"status": ctl.lib.status()})
                        return self._json({"ok": True, "doc": doc})
                    b = self._json_body()
                    if path == "/api/listen":
                        try:
                            ctl.start_listening() if b.get("on") else ctl.stop_listening()
                        except AudioError as e:
                            return self._err(400, str(e))
                        return self._json({"ok": True, "listening": ctl.listening})
                    if path == "/api/channels":
                        ctl.set_channels(b.get("mic"), b.get("loop"))
                        return self._json({"ok": True})
                    if path == "/api/settings":
                        return self._json({"ok": True, "settings": ctl.update_settings(b)})
                    if path == "/api/advice":
                        ctl.request_advice(str(b.get("note") or ""))
                        return self._json({"ok": True})
                    if path == "/api/situation":
                        text = str(b.get("text") or "").strip()
                        if not text:
                            return self._err(400, "Опиши ситуацию или вставь диалог")
                        return self._json({"ok": True, "id": ctl.analyze_situation(text, str(b.get("note") or ""))})
                    if path == "/api/manual":
                        ctl.add_manual(str(b.get("speaker") or "them"), str(b.get("text") or ""))
                        return self._json({"ok": True})
                    if path == "/api/clear":
                        ctl.clear()
                        return self._json({"ok": True})
                    if path == "/api/demo":
                        ctl.run_demo(bool(b.get("on", True)))
                        return self._json({"ok": True})
                    if path == "/api/library/rescan":
                        r = ctl.lib.rescan()
                        ctl.bus.publish("library", {"status": ctl.lib.status()})
                        return self._json({"ok": True, **r})
                    if path == "/api/library/delete":
                        ok = ctl.lib.remove(str(b.get("id") or ""))
                        ctl.bus.publish("library", {"status": ctl.lib.status()})
                        return self._json({"ok": ok})
                    if path == "/api/library/book":
                        ok = ctl.lib.set_book(str(b.get("id") or ""), str(b.get("book") or ""))
                        ctl.bus.publish("library", {"status": ctl.lib.status()})
                        return self._json({"ok": ok})
                    if path == "/api/library/open":
                        _open_folder(paths.library_dir())
                        return self._json({"ok": True})
                    if path == "/api/llm/test":
                        return self._json(ctl.llm_test())
                    if path == "/api/quit":
                        self._json({"ok": True})
                        if srv.on_quit:
                            threading.Thread(target=srv.on_quit, daemon=True).start()
                        return
                except (ValueError, json.JSONDecodeError) as e:
                    return self._err(400, str(e))
                except Exception as e:  # noqa: BLE001
                    return self._err(500, str(e))
                return self._err(404, "not found")

        return Handler


def _open_folder(p: Path) -> None:
    try:
        if sys.platform == "win32":
            os.startfile(str(p))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(p)])
        else:
            subprocess.Popen(["xdg-open", str(p)])
    except Exception:  # noqa: BLE001
        pass
