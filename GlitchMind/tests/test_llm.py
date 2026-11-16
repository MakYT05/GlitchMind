import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from glitchmind.llm import prompts
from glitchmind.llm.client import LLMClient, LLMError, parse_json_loose, strip_think

GOOD = {
    "situation": "Собеседник давит виной.", "their_goal": "уступка",
    "dynamics": [{"label": "Вина", "book": "Сьюзен Форвард", "why": "рычаг FOG"}],
    "replies": [{"style": "Мягко", "technique": "Затуманивание", "book": "Мануэль Смит", "text": "Возможно.", "why": "почему"},
                {"style": "Тактически", "technique": "Метка", "book": "Крисс Восс", "text": "Похоже, тебе обидно.", "why": ""},
                "просто строка"],
    "questions": ["Что именно нужно?"], "avoid": "не оправдываться", "self_coaching": [], "deception": [],
    "safety": "",
}


def test_parse_json_variants():
    assert parse_json_loose('{"a": 1}') == {"a": 1}
    assert parse_json_loose('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json_loose('Вот ответ: {"a": [1, 2,],} конец') == {"a": [1, 2]}
    assert parse_json_loose("<think>hmm {x}</think>{\"ok\": true}") == {"ok": True}
    assert parse_json_loose("вообще не json") is None
    assert parse_json_loose("[1,2]") is None
    assert strip_think("<think>a\nb</think> ответ") == "ответ"


def test_normalize_advice_maps_books_and_tolerates_garbage():
    a = prompts.normalize_advice(GOOD, "qwen")
    assert a["source"] == "llm" and a["model"] == "qwen"
    assert [r["book"] for r in a["replies"]] == ["smith", "voss", ""]
    assert a["dynamics"][0]["book"] == "forward"
    assert a["avoid"] == ["не оправдываться"] and a["safety"] is None
    weird = prompts.normalize_advice({"replies": [{"text": ""}, 5, None], "questions": 7, "dynamics": "x"})
    assert weird["replies"] == [] and weird["questions"] == []


def test_normalize_recognises_cabane_spellings():
    for name in ("Оливия Фокс Кобейн", "Кабейн", "Olivia Cabane"):
        r = prompts.normalize_advice({"replies": [{"text": "x", "book": name}]})["replies"][0]
        assert r["book"] == "cabane", name


def test_prompts_contain_books_and_rules():
    sp = prompts.system_prompt()
    for author in ("Смит", "Форвард", "Томпсон", "Восс", "Кобейн"):
        assert author in sp
    assert "Никогда не утверждай" in sp and "112" in sp and '"replies"' in sp


def test_live_prompt_includes_marks_passages_and_note():
    tr = [{"speaker": "them", "text": "Ты обязан!", "hits": [{"label": "Апелляция к долгу", "group": "manip"}],
           "cues": [], "features": {"latency": 3.2}},
          {"speaker": "me", "text": "Подумаю", "hits": [], "cues": [], "features": {}}]
    msgs = prompts.build_live_messages(tr, [{"doc": "f.txt", "book": "forward", "text": "Фрагмент"}], "отказать мягко")
    user = msgs[1]["content"]
    assert "[Собеседник] Ты обязан!" in user and "Апелляция к долгу" in user and "3.2" in user
    assert "Фрагмент" in user and "отказать мягко" in user and "Сьюзен Форвард" in user


def test_url_builders():
    assert LLMClient("ollama", "http://h:11434/", "m")._ollama_base() == "http://h:11434"
    assert LLMClient("ollama", "http://h:11434/api", "m")._ollama_base() == "http://h:11434"
    assert LLMClient("openai", "http://h:1234", "m")._openai_base() == "http://h:1234/v1"
    assert LLMClient("openai", "http://h/v1/chat/completions", "m")._openai_base() == "http://h/v1"
    assert LLMClient("openai", "http://h/api/v2", "m")._openai_base() == "http://h/api/v2"


class Fake(BaseHTTPRequestHandler):
    calls = []

    def log_message(self, *a):
        pass

    def _reply(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        Fake.calls.append(("GET", self.path, self.headers.get("Authorization")))
        if self.path == "/api/tags":
            return self._reply(200, {"models": [{"name": "qwen2.5:7b"}, {"name": "llama3:8b"}]})
        if self.path == "/v1/models":
            return self._reply(200, {"data": [{"id": "gpt-x"}]})
        self._reply(404, {"error": "nope"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n))
        Fake.calls.append(("POST", self.path, body))
        if self.path == "/api/chat":
            if body["model"] == "missing":
                return self._reply(404, {"error": "model not found"})
            return self._reply(200, {"message": {"content": "<think>x</think>" + json.dumps(GOOD)}})
        if self.path == "/v1/chat/completions":
            if self.headers.get("Authorization") != "Bearer k":
                return self._reply(401, {"error": {"message": "bad key"}})
            return self._reply(200, {"choices": [{"message": {"content": json.dumps(GOOD)}}]})
        self._reply(404, {"error": "nope"})


@pytest.fixture
def fake_server():
    Fake.calls.clear()
    srv = HTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def test_ollama_chat_and_models(fake_server):
    c = LLMClient("ollama", fake_server, "qwen2.5:7b", temperature=0.2, num_ctx=4096)
    assert c.list_models() == ["llama3:8b", "qwen2.5:7b"]
    out = c.chat([{"role": "user", "content": "hi"}], json_mode=True)
    assert parse_json_loose(out)["situation"].startswith("Собеседник")
    sent = [x for x in Fake.calls if x[0] == "POST"][0][2]
    assert sent["format"] == "json" and sent["options"]["num_ctx"] == 4096 and sent["stream"] is False
    assert c.test()["ok"] is True


def test_ollama_missing_model_has_hint(fake_server):
    c = LLMClient("ollama", fake_server, "missing")
    with pytest.raises(LLMError) as e:
        c.chat([{"role": "user", "content": "x"}])
    assert "ollama pull missing" in str(e.value)
    t = LLMClient("ollama", fake_server, "other-model").test()
    assert t["ok"] is False and "other-model" in t["message"] and t["models"]


def test_openai_chat_auth(fake_server):
    ok = LLMClient("openai", fake_server, "gpt-x", api_key="k")
    assert "situation" in ok.chat([{"role": "user", "content": "x"}], json_mode=True)
    with pytest.raises(LLMError) as e:
        LLMClient("openai", fake_server, "gpt-x", api_key="wrong").chat([{"role": "user", "content": "x"}])
    assert "401" in str(e.value) and "ключ" in str(e.value)


def test_connection_refused_message():
    c = LLMClient("ollama", "http://127.0.0.1:9", "m", timeout=2)
    r = c.test()
    assert r["ok"] is False and "Ollama" in r["message"]
    with pytest.raises(LLMError):
        LLMClient("ollama", "http://127.0.0.1:9", "").chat([])  # модель не выбрана
