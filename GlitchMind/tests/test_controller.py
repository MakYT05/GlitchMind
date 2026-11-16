import queue
import time
import weakref

import pytest

from glitchmind.audio.asr import FakeASR
from glitchmind.audio.capture import AudioError
from glitchmind.config import SettingsStore
from glitchmind.controller import Controller
from glitchmind.events import EventBus
from glitchmind.knowledge.library import Library

from conftest import silence, speech


class FakeSource:
    created: dict = {}

    def __init__(self, kind, dev, on_audio, on_error):
        self.kind, self.on_audio, self.running = kind, on_audio, False
        FakeSource.created[kind] = self

    def start(self):
        if getattr(FakeSource, "fail", None) == self.kind:
            raise AudioError("устройство занято")
        self.running = True

    def stop(self):
        self.running = False


@pytest.fixture
def env(tmp_path):
    FakeSource.created = {}
    FakeSource.fail = None
    store = SettingsStore(tmp_path / "c.json")
    bus = EventBus()
    q = bus.subscribe()
    lib = Library(tmp_path / "lib", tmp_path / "idx")
    asr = FakeASR()
    ctl = Controller(store, bus, lib, asr_factory=lambda s, cb: asr, source_factory=FakeSource)
    yield ctl, bus, q, asr, store
    ctl.shutdown()


_buf = weakref.WeakKeyDictionary()


def _fill(q, timeout):
    """Копит ВСЕ события очереди, чтобы ожидание одного типа не выбрасывало остальные."""
    buf = _buf.setdefault(q, [])
    end = time.time() + timeout
    while True:
        try:
            buf.append(q.get(timeout=max(0.01, end - time.time())))
        except queue.Empty:
            pass
        if time.time() >= end:
            return buf


def drain(q, types=None, timeout=0.0):
    buf = _fill(q, timeout)
    out = [m for m in buf if not types or m["type"] in types]
    buf[:] = [m for m in buf if m not in out]
    return out


def wait_for(q, typ, timeout=5.0, pred=lambda d: True):
    end = time.time() + timeout
    while time.time() < end:
        buf = _fill(q, 0.1)
        for m in buf:
            if m["type"] == typ and pred(m["data"]):
                buf.remove(m)
                return m["data"]
    raise AssertionError(f"нет события {typ}")


def test_button_toggles_listening_and_sources(env):
    ctl, bus, q, *_ = env
    ctl.start_listening()
    assert ctl.listening and set(FakeSource.created) == {"mic", "loop"}
    assert all(s.running for s in FakeSource.created.values())
    assert wait_for(q, "state", pred=lambda d: d["listening"])["active"] == {"mic": True, "loop": True}
    ctl.stop_listening()
    assert not ctl.listening and not any(s.running for s in FakeSource.created.values())
    assert wait_for(q, "state", pred=lambda d: not d["listening"])


def test_channels_switch_individually(env):
    ctl, *_ = env
    ctl.set_channels(mic=True, loop=False)
    ctl.start_listening()
    assert set(FakeSource.created) == {"mic"}
    ctl.set_channels(loop=True)  # на лету, с перезапуском
    assert ctl.listening and FakeSource.created["loop"].running


def test_no_channels_is_error(env):
    ctl, *_ = env
    ctl.set_channels(mic=False, loop=False)
    with pytest.raises(AudioError):
        ctl.start_listening()
    assert not ctl.listening


def test_partial_failure_keeps_working_channel(env):
    ctl, bus, q, *_ = env
    FakeSource.fail = "loop"
    ctl.start_listening()
    assert ctl.listening and list(ctl._sources) == ["mic"]
    err = wait_for(q, "error")
    assert "Наушники" in err["message"]
    FakeSource.fail = None
    ctl.stop_listening()
    FakeSource.fail = "mic"
    ctl.set_channels(loop=False)
    with pytest.raises(AudioError):
        ctl.start_listening()


def test_audio_to_transcript_pipeline(env):
    ctl, bus, q, asr, _ = env
    asr.lines = ["Если ты сейчас не согласишься, то можешь больше на меня не рассчитывать."]
    ctl.start_listening()
    src = FakeSource.created["loop"]  # звук из наушников = собеседник
    src.on_audio(speech(1.2), time.time())
    u = wait_for(q, "utterance", timeout=8)
    assert u["speaker"] == "them" and u["source"] == "loop"
    assert "fear_threat" in {h["id"] for h in u["hits"]}
    assert u["features"].get("f0_mean")
    adv = wait_for(q, "advice", timeout=5, pred=lambda a: bool(a["replies"]))
    assert adv["source"] == "rules" and adv["meters"]["pressure"] > 40


def test_mic_is_me_and_latency_measured(env):
    ctl, bus, q, asr, _ = env
    t = time.time()
    ctl._ingest("me", "Мне нужно подумать", {}, t, t, "mic")
    ctl._ingest("them", "Хорошо, жду ответа", {}, t + 2.5, t + 3, "loop")
    them = [u for u in ctl.transcript if u["speaker"] == "them"][0]
    assert 2.4 <= them["features"]["latency"] <= 2.6


def test_echo_from_speakers_is_dropped(env):
    ctl, *_ = env
    t = time.time()
    ctl._ingest("them", "Если ты опять опоздаешь то я уйду", {}, t, t, "loop")
    assert ctl._ingest("me", "если ты опять опоздаешь то я уйду", {}, t + 0.5, t + 1, "mic") is None
    assert ctl._ingest("me", "Хорошо, договорились встретиться", {}, t + 2, t + 3, "mic") is not None
    assert len(ctl.transcript) == 2


def test_manual_clear_and_meters(env):
    ctl, bus, q, *_ = env
    ctl.add_manual("them", "Решай прямо сейчас! Если откажешься — пожалеешь.")
    ctl.add_manual("me", "   ")
    assert len(ctl.transcript) == 1 and ctl.meters["pressure"] > 40
    wait_for(q, "advice", pred=lambda a: bool(a["replies"]))
    ctl.clear()
    assert ctl.transcript == [] and ctl.meters["pressure"] == 0
    assert wait_for(q, "cleared") == {}


def test_auto_advice_off_does_not_trigger(env):
    ctl, bus, q, _, store = env
    store.update({"auto_advice": False})
    ctl.add_manual("them", "Если откажешься — пожалеешь")
    assert drain(q, ["advice"], 0.6) == []
    ctl.request_advice("мягко")
    assert wait_for(q, "advice")["note"] == "мягко"


def test_situation_analysis_event(env):
    ctl, bus, q, *_ = env
    sid = ctl.analyze_situation("Он: Ты обязан приехать. Нормальные люди так делают.\nЯ: Мне нужно подумать", "отказать")
    s = wait_for(q, "situation", pred=lambda d: d.get("id") == sid)
    assert s["source"] == "rules" and s["replies"] and s["note"] == "отказать"


def test_llm_failure_keeps_rule_advice(env):
    ctl, bus, q, _, store = env
    store.update({"llm_model": "x", "llm_url": "http://127.0.0.1:9", "llm_timeout": 2})
    ctl.add_manual("them", "Решай прямо сейчас!")
    st = wait_for(q, "status", timeout=10, pred=lambda d: d["kind"] == "llm" and d["state"] == "error")
    assert "Ollama" in st["message"]
    assert ctl.last_advice["source"] == "rules" and ctl.last_advice["replies"]


def test_llm_success_merges_with_rules(env, monkeypatch):
    from glitchmind.llm import client as cl
    ctl, bus, q, _, store = env
    ctl.lib.add_bytes("Форвард.txt", "Шантажист давит на чувство вины и страх.".encode())
    store.update({"llm_model": "fake"})
    seen = {}

    def fake_chat(self, messages, json_mode=False):
        seen["user"] = messages[1]["content"]
        return '{"situation": "Глубокий разбор", "replies": [{"style": "Мягко", "technique": "T", "book": "Форвард", "text": "Ответ", "why": ""}]}'

    monkeypatch.setattr(cl.LLMClient, "chat", fake_chat)
    ctl.add_manual("them", "Ты обязан! Нормальные люди так не поступают, чувство вины")
    a = wait_for(q, "advice", timeout=8, pred=lambda a: a["source"] == "llm")
    assert a["situation"] == "Глубокий разбор" and a["replies"][0]["book"] == "forward"
    assert a["meters"] and a["sources"] and a["sources"][0]["book"] == "Сьюзен Форвард"
    assert "Фрагменты из книг" in seen["user"]


def test_llm_unparsed_text_is_shown(env, monkeypatch):
    from glitchmind.llm import client as cl
    ctl, bus, q, _, store = env
    store.update({"llm_model": "fake"})
    monkeypatch.setattr(cl.LLMClient, "chat", lambda self, m, json_mode=False: "Просто текст без json")
    ctl.add_manual("them", "Решай прямо сейчас!")
    a = wait_for(q, "advice", timeout=8, pred=lambda a: a.get("unparsed"))
    assert "Просто текст" in a["situation"] and a["replies"]  # правила подставлены вместо пустоты


def test_settings_update_restarts_audio(env):
    ctl, *_ = env
    ctl.start_listening()
    first = FakeSource.created["mic"]
    ctl.update_settings({"mic_device": "USB Mic"})
    assert not first.running and FakeSource.created["mic"].running and ctl.listening
    out = ctl.update_settings({"vad_sensitivity": 0.9, "llm_api_key": "abc"})
    assert out["vad_sensitivity"] == 0.9 and "llm_api_key" not in out and out["llm_api_key_set"]


def test_demo_runs_and_stops(env, monkeypatch):
    from glitchmind import controller as c
    monkeypatch.setattr(c, "DEMO_SCRIPT", [(0.0, "them", "Если ты уйдёшь, пожалеешь", {}), (0.0, "me", "Понимаю", {})])
    ctl, bus, q, *_ = env
    ctl.run_demo(True)
    wait_for(q, "demo", pred=lambda d: d["on"] is False)
    assert len(ctl.transcript) == 2
    assert ctl.state()["techniques"] == 39
