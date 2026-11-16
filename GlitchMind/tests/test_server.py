import http.client
import json
import threading

import pytest

from glitchmind import paths
from glitchmind.config import SettingsStore
from glitchmind.controller import Controller
from glitchmind.events import EventBus
from glitchmind.knowledge.library import Library
from glitchmind.server import ApiServer
from glitchmind.audio.asr import FakeASR

from test_controller import FakeSource


@pytest.fixture
def srv(home):
    store = SettingsStore(home / "c.json")
    lib = Library(home / "lib", home / "idx")
    ctl = Controller(store, EventBus(), lib, asr_factory=lambda s, cb: FakeASR(), source_factory=FakeSource)
    quit_evt = threading.Event()
    s = ApiServer(ctl, on_quit=quit_evt.set)
    s.quit_evt = quit_evt
    s.start()
    yield s
    s.stop()
    ctl.shutdown()


def req(s, method, path, body=None, headers=None, token=True, raw=None, host=None):
    h = dict(headers or {})
    if token:
        h["X-Token"] = s.token
    h["Host"] = host or f"127.0.0.1:{s.port}"
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    if data is not None and raw is None:
        h["Content-Type"] = "application/json"
    c = http.client.HTTPConnection("127.0.0.1", s.port, timeout=5)
    c.putrequest(method, path, skip_host=True)
    for k, v in h.items():
        c.putheader(k, v)
    c.putheader("Content-Length", str(len(data or b"")))
    c.endheaders(data)
    r = c.getresponse()
    payload = r.read()
    try:
        return r.status, json.loads(payload)
    except ValueError:
        return r.status, payload.decode("utf-8", "replace")


def test_index_requires_token_and_embeds_it(srv):
    assert req(srv, "GET", "/", token=False)[0] == 403
    code, html = req(srv, "GET", f"/?t={srv.token}", token=False)
    assert code == 200 and srv.token in html and "{{GM_TOKEN}}" not in html
    assert f'window.__TOKEN__ = "{srv.token}"' in html  # имя переменной не затронуто подстановкой


def test_api_forbidden_without_token_or_with_bad_host(srv):
    assert req(srv, "GET", "/api/state", token=False)[0] == 403
    assert req(srv, "GET", "/api/state", headers={"X-Token": "wrong"}, token=False)[0] == 403
    assert req(srv, "GET", "/api/state", host="evil.example.com")[0] == 403  # защита от DNS rebinding
    assert req(srv, "POST", "/api/clear", {}, token=False)[0] == 403


def test_cross_origin_post_blocked(srv):
    assert req(srv, "POST", "/api/clear", {}, headers={"Origin": "http://evil.example.com"})[0] == 403
    assert req(srv, "POST", "/api/clear", {}, headers={"Origin": f"http://127.0.0.1:{srv.port}"})[0] == 200


def test_state_shape(srv):
    code, st = req(srv, "GET", "/api/state")
    assert code == 200
    assert {"listening", "channels", "transcript", "meters", "settings", "library", "techniques", "has_skin"} <= st.keys()
    assert st["techniques"] == 39 and "llm_api_key" not in st["settings"]


def test_listen_toggle_and_errors(srv):
    assert req(srv, "POST", "/api/listen", {"on": True})[1]["listening"] is True
    assert req(srv, "GET", "/api/state")[1]["active"] == {"mic": True, "loop": True}
    assert req(srv, "POST", "/api/listen", {"on": False})[1]["listening"] is False
    req(srv, "POST", "/api/channels", {"mic": False, "loop": False})
    code, err = req(srv, "POST", "/api/listen", {"on": True})
    assert code == 400 and "хотя бы один" in err["error"]


def test_manual_settings_situation_endpoints(srv):
    assert req(srv, "POST", "/api/manual", {"speaker": "them", "text": "Решай прямо сейчас!"})[0] == 200
    st = req(srv, "GET", "/api/state")[1]
    assert st["transcript"][0]["hits"][0]["id"] in ("pressure", "fear_threat") and st["meters"]["pressure"] > 0
    out = req(srv, "POST", "/api/settings", {"language": "en", "llm_api_key": "zzz"})[1]["settings"]
    assert out["language"] == "en" and out["llm_api_key_set"] is True and "llm_api_key" not in out
    assert req(srv, "POST", "/api/situation", {"text": "  "})[0] == 400
    assert req(srv, "POST", "/api/situation", {"text": "Он: ты обязан"})[1]["ok"]
    assert req(srv, "POST", "/api/nonexistent", {})[0] == 404
    assert req(srv, "POST", "/api/settings", raw=b"{not json")[0] == 400


def test_library_upload_search_delete(srv):
    text = ("Шантажист давит на чувство вины и страх. " * 8).encode("cp1251")
    code, r = req(srv, "POST", "/api/library/upload?name=" + "%D0%A4%D0%BE%D1%80%D0%B2%D0%B0%D1%80%D0%B4.txt", raw=text)
    assert code == 200 and r["doc"]["book"] == "forward"
    res = req(srv, "GET", "/api/library/search?q=" + "%D1%87%D1%83%D0%B2%D1%81%D1%82%D0%B2%D0%BE+%D0%B2%D0%B8%D0%BD%D1%8B")[1]["results"]
    assert res and "вины" in res[0]["text"]
    assert req(srv, "POST", "/api/library/upload?name=a.exe", raw=b"x")[0] == 400
    assert req(srv, "POST", "/api/library/book", {"id": r["doc"]["id"], "book": "voss"})[1]["ok"]
    assert req(srv, "POST", "/api/library/delete", {"id": r["doc"]["id"]})[1]["ok"]
    assert req(srv, "GET", "/api/library")[1]["docs"] == []


def test_static_files_and_path_traversal(srv):
    assert req(srv, "GET", "/static/style.css", token=False)[0] == 200
    assert req(srv, "GET", "/static/app.js", token=False)[0] == 200
    assert req(srv, "GET", "/static/../server.py", token=False)[0] == 404
    assert req(srv, "GET", "/static/%2e%2e/server.py", token=False)[0] == 404
    assert req(srv, "GET", "/static/nope.js", token=False)[0] == 404


def test_skin_endpoint(srv, home):
    assert req(srv, "GET", "/skin")[0] == 404
    (home / "skin.png").write_bytes(b"\x89PNG fake")
    assert paths.skin_file() is not None
    assert req(srv, "GET", "/api/state")[1]["has_skin"] is True


def test_kb_and_devices_endpoints(srv):
    books = req(srv, "GET", "/api/kb")[1]["books"]
    assert [b["id"] for b in books] == ["smith", "forward", "thompson", "voss", "cabane"]
    assert "backend" in req(srv, "GET", "/api/devices")[1]
    assert req(srv, "GET", "/api/llm/models")[1]["ok"] is False  # Ollama не запущена


def test_sse_stream_delivers_events(srv):
    c = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=5)
    c.request("GET", f"/events?t={srv.token}", headers={"Host": f"127.0.0.1:{srv.port}"})
    r = c.getresponse()
    assert r.status == 200 and r.getheader("Content-Type").startswith("text/event-stream")
    r.fp.readline()  # ": hello"
    r.fp.readline()
    threading.Timer(0.2, lambda: req(srv, "POST", "/api/manual", {"speaker": "them", "text": "Привет"})).start()
    seen = set()
    for _ in range(20):
        line = r.fp.readline().decode()
        if line.startswith("event:"):
            seen.add(line.split(":", 1)[1].strip())
        if {"utterance", "meters"} <= seen:
            break
    c.close()
    assert {"utterance", "meters"} <= seen


def test_quit_endpoint_calls_callback(srv):
    assert req(srv, "POST", "/api/quit", {})[1]["ok"]
    assert srv.quit_evt.wait(2)
